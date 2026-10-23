"""`services/ingest/osm_power.py`: the OpenStreetMap lines and substations load as assets with
provenance per record, a reload is idempotent, and an element a covered region's new extract no
longer has is withdrawn -- never one in a region the new build does not cover, and never more than
the churn guard allows (lane grid-osm, 2026-10-22)."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.context import osm_power as op
from services.db.models import Asset, InterconnectionPoint, Licence, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id
from services.ingest import context_layers
from services.ingest import osm_power as loader
from services.ingest.assets import UnsupportedAssetTypeError, load_assets

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

TS = "2026-10-21T20:21:02Z"


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _lines(
    region: str = "US-TX", ids: range = range(1, 21), *, scope: dict[int, list[str]] | None = None
) -> pd.DataFrame:
    """Line rows read from `region`'s extract; `scope` overrides a row's `scope_regions` (and makes
    its `region` the first of them), as the build labels a way across a state line."""
    rows = [
        op.line_row(
            op.LineRecord(
                "way",
                i,
                {"power": "line", "voltage": "138000", "operator": "Oncor"},
                [(-97.0, 30.0 + i / 100), (-97.1, 30.0 + i / 100)],
            ),
            region,
            TS,
        )
        for i in ids
    ]
    for row in rows:
        if scope and row["osm_id"] in scope:
            row["scope_regions"] = row["states_crossed"] = scope[row["osm_id"]]
            row["region"] = scope[row["osm_id"]][0]
    return op.normalise_lines(rows, licence=op._licence_text())


def _substations(region: str = "US-TX", ids: range = range(101, 121)) -> pd.DataFrame:
    rows = [
        op.substation_row(
            op.SubstationRecord(
                "node", i, {"power": "substation", "voltage": "138000;12470"}, (-97.0, 31.0 + i / 1000)
            ),
            region,
            TS,
        )
        for i in ids
    ]
    return op.normalise_substations(rows, licence=op._licence_text())


def test_lines_and_substations_load_with_provenance_and_the_osm_credit(session):
    lines = loader.load_osm_power_frame(session, _lines(), "transmission_line")
    subs = loader.load_osm_power_frame(session, _substations(), "substation")
    assert (lines.loaded["inserted"], subs.loaded["inserted"]) == (20, 20)
    row = session.scalars(select(Asset).where(Asset.source_asset_id == "way/1")).one()
    assert row.asset_type == "transmission_line" and row.source_id == "us.osm.power"
    assert row.source_url == "https://www.openstreetmap.org/way/1"
    assert row.retrieved_at.replace(tzinfo=dt.UTC) == dt.datetime(2026, 10, 21, 20, 21, 2, tzinfo=dt.UTC)
    assert row.geom_line is not None and row.attributes["voltage_kv"] == 138.0
    sub = session.scalars(select(Asset).where(Asset.source_asset_id == "node/101")).one()
    assert sub.asset_type == "substation" and sub.geom is not None and sub.geom_line is None
    assert sub.attributes["voltages_kv"] == [138.0, 12.47]
    licence = session.get(Licence, row.licence_id)
    assert licence is not None and licence.reuse_class == "attribution" and licence.share_alike
    assert "© OpenStreetMap contributors" in (licence.attribution_text or "")
    source = session.get(Source, "us.osm.power")
    assert source is not None and "© OpenStreetMap contributors" in (
        source.attribution_text or licence.attribution_text
    )


def test_a_reload_inserts_nothing_and_keeps_every_identity(session):
    loader.load_osm_power_frame(session, _lines(), "transmission_line")
    before = {a.source_asset_id: (a.public_id, a.slug) for a in session.scalars(select(Asset))}
    again = loader.load_osm_power_frame(session, _lines(), "transmission_line")
    assert (again.loaded["inserted"], again.loaded["updated"], again.withdrawn) == (0, 20, 0)
    assert {a.source_asset_id: (a.public_id, a.slug) for a in session.scalars(select(Asset))} == before


def test_an_element_the_new_extract_lacks_is_withdrawn_and_unlinked(session):
    loader.load_osm_power_frame(session, _substations(), "substation")
    gone = session.scalars(select(Asset).where(Asset.source_asset_id == "node/101")).one()
    lic_id = gone.licence_id
    point = InterconnectionPoint(
        public_id="",
        operator="ERCOT",
        name_display="Test 138kV",
        name_key="sub:test|138",
        key_rule="t",
        kind="substation",
        source_id="us.osm.power",
        source_url="https://example.test",
        retrieved_at=dt.datetime(2026, 10, 1, tzinfo=dt.UTC),
        licence_id=lic_id,
        substation_asset_id=gone.id,
    )
    session.add(point)
    session.flush()
    point.public_id = public_id("poi", point.id)
    session.commit()
    result = loader.load_osm_power_frame(session, _substations(ids=range(102, 121)), "substation")
    assert (result.withdrawn, result.withdrawn_sample, result.interconnection_points_unlinked) == (
        1,
        ["node/101"],
        1,
    )
    assert session.scalars(select(Asset).where(Asset.source_asset_id == "node/101")).first() is None
    assert session.get(InterconnectionPoint, point.id).substation_asset_id is None
    assert result.covered_regions == ["US-TX"]


def test_nothing_is_withdrawn_in_a_region_the_new_build_does_not_cover(session):
    loader.load_osm_power_frame(session, _lines("US-OK", range(1, 11)), "transmission_line")
    result = loader.load_osm_power_frame(session, _lines("US-TX", range(11, 21)), "transmission_line")
    assert result.withdrawn == 0 and result.covered_regions == ["US-TX"]
    assert len(session.scalars(select(Asset)).all()) == 20


def test_the_manifest_names_the_covered_regions_when_it_is_there(session):
    loader.load_osm_power_frame(session, _lines("US-OK", range(1, 21)), "transmission_line")
    empty_tx: dict[str, Any] = {"regions": [{"region": "US-TX"}]}
    result = loader.load_osm_power_frame(
        session, _lines("US-TX", range(21, 22)), "transmission_line", manifest=empty_tx
    )
    assert result.covered_regions == ["US-TX"] and result.withdrawn == 0


def test_the_churn_guard_holds_a_region_that_would_lose_too_much(session):
    loader.load_osm_power_frame(session, _lines(), "transmission_line")
    result = loader.load_osm_power_frame(session, _lines(ids=range(1, 11)), "transmission_line", min_rows=5)
    assert result.withdrawn == 0
    assert result.held_regions == {"US-TX": {"would_withdraw": 10, "held": 20}}
    assert len(session.scalars(select(Asset)).all()) == 20
    passed = loader.load_osm_power_frame(
        session, _lines(ids=range(1, 11)), "transmission_line", max_share=0.6, min_rows=5
    )
    assert passed.withdrawn == 10


def test_a_few_withdrawals_in_a_small_region_are_not_held(session):
    """Review 2026-10-22: five ordinary deletions out of twenty rows is 25%, but under the 50-row
    floor, so they are withdrawn rather than held for a person."""
    loader.load_osm_power_frame(session, _lines(), "transmission_line")
    result = loader.load_osm_power_frame(session, _lines(ids=range(1, 16)), "transmission_line")
    assert (result.withdrawn, result.held_regions) == (5, {})
    assert len(session.scalars(select(Asset)).all()) == 15


def test_the_churn_guard_counts_the_rows_held_before_this_load(session):
    """Review 2026-10-22: the share is of what the region held before the load, so a load that also
    inserts many new rows cannot dilute 70 withdrawals of 600 (11.7%) into 70 of 1,530 (4.6%)."""
    loader.load_osm_power_frame(session, _lines(ids=range(1, 601)), "transmission_line")
    kept_and_new = pd.concat([_lines(ids=range(71, 601)), _lines(ids=range(1001, 2001))], ignore_index=True)
    result = loader.load_osm_power_frame(session, kept_and_new, "transmission_line")
    assert result.withdrawn == 0
    assert result.held_regions == {"US-TX": {"would_withdraw": 70, "held": 600}}
    assert result.loaded["inserted"] == 1000


def test_withdrawal_follows_the_scope_rule_across_a_state_line(session):
    """Review 2026-10-22: a way across the Red River is labelled Oklahoma but in Texas's scope, so a
    Texas load that no longer has it withdraws it, while a way wholly in Oklahoma, out of the load's
    scope, is never touched."""
    texas = {"regions": [{"region": "US-TX"}]}
    loader.load_osm_power_frame(
        session,
        _lines("US-OK", range(1, 3)),
        "transmission_line",
        manifest={"regions": [{"region": "US-OK"}]},
    )
    border = {5: ["US-OK", "US-TX"]}
    first = loader.load_osm_power_frame(
        session, _lines(ids=range(3, 9), scope=border), "transmission_line", manifest=texas
    )
    assert first.loaded["inserted"] == 6
    row = session.scalars(select(Asset).where(Asset.source_asset_id == "way/5")).one()
    assert row.state_code == "US-OK" and row.attributes["scope_regions"] == ["US-OK", "US-TX"]
    later = loader.load_osm_power_frame(session, _lines(ids=range(6, 9)), "transmission_line", manifest=texas)
    assert (later.withdrawn, later.withdrawn_sample) == (3, ["way/3", "way/4", "way/5"])
    left = sorted(a.source_asset_id for a in session.scalars(select(Asset)))
    assert left == ["way/1", "way/2", "way/6", "way/7", "way/8"]


def test_narrowing_the_loaded_regions_removes_the_rows_no_load_covers_any_more(session):
    """Review 2026-10-22: narrowing `GRID_LAYERS_REGIONS` from US-OK,US-TX to US-TX left Oklahoma's
    rows in the store, never refreshed, still counted as covered by the map's source rule. A load
    narrowed to named regions now removes the rows in none of their scopes; a way across the state
    line stays, because it is in Texas's scope. A load that was not narrowed removes nothing."""
    both = {"regions": [{"region": "US-OK"}, {"region": "US-TX"}], "regions_loaded": ["US-OK", "US-TX"]}
    border = {21: ["US-OK", "US-TX"]}
    frame = pd.concat(
        [_lines("US-OK", range(1, 11)), _lines("US-TX", range(11, 22), scope=border)], ignore_index=True
    )
    loader.load_osm_power_frame(session, frame, "transmission_line", manifest=both)
    texas_rows = _lines("US-TX", range(11, 22), scope=border)
    not_narrowed = {"regions": [{"region": "US-TX"}], "regions_loaded": None}
    kept = loader.load_osm_power_frame(session, texas_rows, "transmission_line", manifest=not_narrowed)
    assert (kept.unloaded_removed, len(session.scalars(select(Asset)).all())) == (0, 21)
    narrowed = {"regions": [{"region": "US-TX"}], "regions_loaded": ["US-TX"]}
    result = loader.load_osm_power_frame(session, texas_rows, "transmission_line", manifest=narrowed)
    assert (result.unloaded_removed, result.unloaded_regions, result.withdrawn) == (10, {"US-OK": 10}, 0)
    left = {a.source_asset_id for a in session.scalars(select(Asset))}
    assert left == {f"way/{i}" for i in range(11, 22)}


def _frames(geo_dir, out_dir) -> None:
    """Write the asset frames for twenty Texas lines and twenty substations through the pipeline's
    own writer (`write_asset_frames`), which stamps them with one `frame_set` token."""
    lines = [
        op.line_row(
            op.LineRecord(
                "way", i, {"power": "line", "voltage": "138000"}, [(-97.0, 30.0 + i / 100), (-97.1, 30.0)]
            ),
            "US-TX",
            TS,
        )
        for i in range(1, 21)
    ]
    subs = [
        op.substation_row(
            op.SubstationRecord("node", i, {"power": "substation"}, (-97.0, 31.0 + i / 1000)), "US-TX", TS
        )
        for i in range(101, 121)
    ]
    files = {
        op.GEO_LINES_NAME: op.write_geoparquet(lines, geo_dir / op.GEO_LINES_NAME, kind="lines"),
        op.GEO_SUBSTATIONS_NAME: op.write_geoparquet(
            subs, geo_dir / op.GEO_SUBSTATIONS_NAME, kind="substations"
        ),
    }
    manifest = {"version": "v1/test", "regions": [{"region": "US-TX"}], "files": files}
    (geo_dir / op.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    op.write_asset_frames(geo_dir, out_dir, with_states=False, licence=op._licence_text())


def test_frames_from_two_different_writes_load_nothing(session, tmp_path):
    """Review 2026-10-22: a crash between the three writes could pair new frames with the previous
    manifest's covered regions. Files whose `frame_set` tokens differ now load and withdraw nothing."""
    first, second = tmp_path / "first", tmp_path / "second"
    _frames(tmp_path / "geo", first)
    _frames(tmp_path / "geo", second)
    assert loader.frame_set_of(first / loader.LINES_FILE) != loader.frame_set_of(second / loader.LINES_FILE)
    mixed = tmp_path / "mixed"
    mixed.mkdir()
    for name in (loader.LINES_FILE, loader.MANIFEST_FILE):
        (mixed / name).write_bytes((first / name).read_bytes())
    (mixed / loader.SUBSTATIONS_FILE).write_bytes((second / loader.SUBSTATIONS_FILE).read_bytes())
    assert loader.load_osm_power(session, mixed) == []
    assert session.scalars(select(Asset)).all() == []
    whole = loader.load_osm_power(session, second)
    assert [(r.asset_type, r.loaded["inserted"]) for r in whole] == [
        ("transmission_line", 20),
        ("substation", 20),
    ]


def test_an_element_retagged_from_substation_to_line_is_replaced_not_collided(session):
    subs = op.normalise_substations(
        [
            op.substation_row(
                op.SubstationRecord("way", 7, {"power": "substation"}, (-97.0, 30.07)), "US-TX", TS
            )
        ],
        licence=op._licence_text(),
    )
    loader.load_osm_power_frame(session, subs, "substation")
    result = loader.load_osm_power_frame(session, _lines(ids=range(1, 11)), "transmission_line")
    assert result.retyped == 1 and result.loaded["inserted"] == 10
    (row,) = session.scalars(select(Asset).where(Asset.source_asset_id == "way/7")).all()
    assert row.asset_type == "transmission_line"


def test_substation_accepts_only_openstreetmap(session):
    frame = _substations().assign(source_id="us.dhs.hifld.electric_substations")
    with pytest.raises(UnsupportedAssetTypeError):
        load_assets(session, frame, "substation")
    with pytest.raises(UnsupportedAssetTypeError):
        load_assets(
            session, _lines().iloc[:0], "substation", source_id="us.lbnl.ferc_hifld_transmission_lines"
        )


def test_context_load_reads_both_files_and_the_manifest(session, tmp_path, monkeypatch):
    directory = tmp_path / "normalized" / "context"
    directory.mkdir(parents=True)
    from pipeline.connectors.base import to_parquet_safe

    to_parquet_safe(_lines()).to_parquet(directory / loader.LINES_FILE, index=False)
    to_parquet_safe(_substations()).to_parquet(directory / loader.SUBSTATIONS_FILE, index=False)
    (directory / loader.MANIFEST_FILE).write_text('{"regions": [{"region": "US-TX"}]}', encoding="utf-8")
    results = loader.load_osm_power(session, directory)
    assert [(r.asset_type, r.loaded["inserted"]) for r in results] == [
        ("transmission_line", 20),
        ("substation", 20),
    ]
    assert loader.load_osm_power(session, tmp_path / "missing") == []
    # The context load (`context_layers.load_asset_layers`, what `context_load` runs) reaches the same
    # loader: a second pass over the same files inserts nothing.
    context_layers.load_asset_layers(session, tmp_path)
    counts = {
        t: len(
            session.scalars(
                select(Asset.id).where(Asset.source_id == op.SOURCE_ID, Asset.asset_type == t)
            ).all()
        )
        for t in ("transmission_line", "substation")
    }
    assert counts == {"transmission_line": 20, "substation": 20}
