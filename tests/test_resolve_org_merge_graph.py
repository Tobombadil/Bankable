"""Organisation merges carry the whole graph, and unmerge restores it exactly (docs/22 §20;
docs/21 §6.3 invariant M1).

Before 2026-09-26 `merge_organization` re-pointed `proposal.sponsor_org_id` and nothing else, so
an organisation holding only `asset_owner` edges (every midstream operator, every EIA-860 owner)
lost its assets from every company page on merge: the edges stayed on a row the ownership tree
excludes (`services/api/orgtree.py::_children_of`), and the event did not list them, so unmerge
could not bring them back either. These tests pin the four things that now move -- ownership
edges, child organisations, the absorbed row's own aliases, and the parent link -- plus the
collision rule and backward compatibility with events written in the old shape.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import SourceEntry
from services.api.assets import organization_asset_totals
from services.api.orgtree import org_scope
from services.db.models import Asset, AssetOwner, Event, Organization, OrganizationAlias, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id, slugify
from services.ingest.loader import upsert_licence_and_source
from services.ingest.organizations import load_merges, read_merge_rules
from services.resolve import merge as merge_mod

UTC = dt.UTC
RETRIEVED = dt.datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


@pytest.fixture()
def session() -> Session:
    import services.resolve.models  # noqa: F401 -- register resolution_decision on Base.metadata

    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def make_source(session: Session, id_: str) -> Source:
    entry = SourceEntry.from_yaml(
        {
            "id": id_,
            "name": f"Test {id_}",
            "jurisdiction": "US",
            "category": "registry",
            "operator": "Test agency",
            "url": f"https://example.org/{id_}",
            "access": "bulk_file",
            "reuse": "open",
            "cadence": "annual",
            "tier": 1,
            "license": f"licence for {id_}",
        }
    )
    return upsert_licence_and_source(session, entry, "test-manifest")


def make_org(session: Session, name: str, *, parent: Organization | None = None) -> Organization:
    org = Organization(
        public_id="", slug="", name_canonical=name, name_normalised=name.lower(), type="other", country="US"
    )
    session.add(org)
    session.flush()
    org.public_id = public_id("org", org.id)
    org.slug = f"{slugify(name)}-{org.public_id[-6:].lower()}"
    if parent is not None:
        if session.get(Source, "curated.test.parents") is None:
            make_source(session, "curated.test.parents")
        org.parent_org_id = parent.id
        org.parent_source_id = "curated.test.parents"
    session.flush()
    return org


def make_asset(session: Session, source: Source, key: str, asset_type: str = "gas_storage") -> Asset:
    asset = Asset(
        public_id=f"ast_{key}",
        slug=f"asset-{key}",
        asset_type=asset_type,
        source_asset_id=key,
        name=f"Asset {key}",
        country="US",
        source_id=source.id,
        source_url=f"{source.url}/{key}",
        retrieved_at=RETRIEVED,
        licence_id=source.licence_id,
    )
    session.add(asset)
    session.flush()
    return asset


def make_edge(
    session: Session,
    asset: Asset,
    org: Organization,
    source: Source,
    *,
    role: str = "operator",
    share_pct: float | None = None,
) -> AssetOwner:
    edge = AssetOwner(
        asset_id=asset.id,
        organization_id=org.id,
        role=role,
        share_pct=share_pct,
        owner_name_raw=org.name_canonical,
        source_id=source.id,
        source_url=f"{source.url}/{asset.source_asset_id}",
        retrieved_at=RETRIEVED,
        licence_id=source.licence_id,
    )
    session.add(edge)
    session.flush()
    return edge


def make_alias(session: Session, org: Organization, source: Source, spelling: str) -> OrganizationAlias:
    alias = OrganizationAlias(
        organization_id=org.id,
        alias=spelling,
        alias_normalised=spelling.lower(),
        kind="filing_spelling",
        source_id=source.id,
        source_url=source.url,
        retrieved_at=RETRIEVED,
        licence_id=source.licence_id,
        confidence=0.9,
        created_by="pipeline",
    )
    session.add(alias)
    session.flush()
    return alias


def scope_edges(session: Session, org: Organization) -> set[uuid.UUID]:
    ids = org_scope(session, org, "all").ids
    return set(session.scalars(select(AssetOwner.id).where(AssetOwner.organization_id.in_(ids))))


@pytest.fixture()
def tallgrass(session: Session) -> dict[str, object]:
    """The measured case, at unit scale: a group parent over two spellings of one subsidiary, one of
    which holds the storage-field edge and nothing else (no proposal, so no alias provenance under
    the old rule)."""
    storage = make_source(session, "src.atlas.storage")
    pipelines = make_source(session, "src.atlas.pipelines")
    group = make_org(session, "Tallgrass Energy")
    survivor = make_org(session, "Tallgrass Interstate Gas Transmission", parent=group)
    absorbed = make_org(session, "TALLGRASS INTERSTATE GAS TRANSMISSIO", parent=group)
    pipe = make_asset(session, pipelines, "TIGT", asset_type="gas_pipeline")
    huntsman = make_asset(session, storage, "HUNTSMAN")
    survivor_edge = make_edge(session, pipe, survivor, pipelines)
    absorbed_edge = make_edge(session, huntsman, absorbed, storage)
    own_alias = make_alias(session, absorbed, storage, "TALLGRASS INTERSTATE GAS TRANSMISSIO")
    return {
        "group": group,
        "survivor": survivor,
        "absorbed": absorbed,
        "survivor_edge": survivor_edge,
        "absorbed_edge": absorbed_edge,
        "own_alias": own_alias,
        "storage": storage,
    }


# ------------------------------------------------------------------- an edge-only organisation
def test_edge_only_merge_moves_the_edge_and_the_tree_still_sees_it(
    session: Session, tallgrass: dict[str, object]
) -> None:
    group, survivor, absorbed = tallgrass["group"], tallgrass["survivor"], tallgrass["absorbed"]
    absorbed_edge, own_alias = tallgrass["absorbed_edge"], tallgrass["own_alias"]
    assert isinstance(group, Organization) and isinstance(survivor, Organization)
    assert isinstance(absorbed, Organization) and isinstance(absorbed_edge, AssetOwner)
    assert isinstance(own_alias, OrganizationAlias)

    edges_before = scope_edges(session, group)
    assert len(edges_before) == 2
    assert org_scope(session, group, "all").organizations == 3
    snapshot = merge_mod.serialize_row(absorbed)

    event = merge_mod.merge_organization(session, canonical=survivor, absorbed=absorbed, rationale="10-K")

    assert event.before is not None
    assert event.before["absorbed"]["asset_owner_ids"] == [str(absorbed_edge.id)]
    assert event.before["absorbed"]["organization_alias_ids"] == [str(own_alias.id)]
    session.refresh(absorbed_edge)
    assert absorbed_edge.organization_id == survivor.id
    # the tree drops the redirect but keeps every edge: one organisation fewer, the same edges
    assert org_scope(session, group, "all").organizations == 2
    assert scope_edges(session, group) == edges_before
    totals = organization_asset_totals(session, group, scope="all")
    assert totals["assets"] == 2
    assert [row["assets"] for row in totals["by_organization"]] == [2]

    # the absorbed row's own alias moved, with its own provenance; no second alias was written
    session.refresh(own_alias)
    assert own_alias.organization_id == survivor.id
    assert own_alias.source_id == "src.atlas.storage"
    assert "written_alias_id" not in (event.after or {})

    merge_mod.unmerge_organization(session, event.id)
    session.refresh(absorbed_edge)
    session.refresh(own_alias)
    assert absorbed_edge.organization_id == absorbed.id
    assert own_alias.organization_id == absorbed.id
    restored = session.get(Organization, absorbed.id)
    assert restored is not None and merge_mod.serialize_row(restored) == snapshot
    assert scope_edges(session, group) == edges_before
    assert org_scope(session, group, "all").organizations == 3


def test_edge_only_merge_writes_the_spelling_alias_from_the_edge_source(session: Session) -> None:
    """No proposal and no alias of its own: the spelling alias takes the edge's provenance (under
    the old rule, which read only `proposal_source`, no alias was written at all)."""
    src = make_source(session, "us.eia.860.test")
    survivor = make_org(session, "Green Knight Economic Development Corporation")
    absorbed = make_org(session, "Green Knight Economic Development Corpor")
    plant = make_asset(session, src, "GK1", asset_type="power_plant")
    make_edge(session, plant, absorbed, src, role="owner", share_pct=100.0)

    event = merge_mod.merge_organization(session, canonical=survivor, absorbed=absorbed, rationale="gkedc")

    assert event.after is not None
    alias = session.get(OrganizationAlias, uuid.UUID(event.after["written_alias_id"]))
    assert alias is not None
    assert alias.organization_id == survivor.id
    assert alias.alias == "Green Knight Economic Development Corpor"
    assert (alias.source_id, alias.source_url) == (src.id, f"{src.url}/GK1")


# --------------------------------------------------------------------------- one with children
def test_children_follow_the_merge_and_come_back_on_unmerge(session: Session) -> None:
    src = make_source(session, "src.children")
    survivor = make_org(session, "Acme Holdings LLC")
    absorbed = make_org(session, "ACME HOLDINGS")
    child_a = make_org(session, "Acme Pipeline Co", parent=absorbed)
    child_b = make_org(session, "Acme Storage Co", parent=absorbed)
    pipe = make_asset(session, src, "P1", asset_type="gas_pipeline")
    make_edge(session, pipe, child_a, src)
    assert org_scope(session, survivor, "all").organizations == 1

    event = merge_mod.merge_organization(session, canonical=survivor, absorbed=absorbed, rationale="r")

    assert event.before is not None
    assert sorted(event.before["absorbed"]["child_organization_ids"]) == sorted(
        [str(child_a.id), str(child_b.id)]
    )
    session.refresh(child_a)
    session.refresh(child_b)
    assert child_a.parent_org_id == survivor.id and child_b.parent_org_id == survivor.id
    # the provenance of the parent claim is not rewritten, only its target
    assert child_a.parent_source_id == "curated.test.parents"
    scope = org_scope(session, survivor, "all")
    assert set(scope.ids) == {survivor.id, child_a.id, child_b.id}
    assert organization_asset_totals(session, survivor, scope="all")["assets"] == 1

    merge_mod.unmerge_organization(session, event.id)
    session.refresh(child_a)
    session.refresh(child_b)
    assert child_a.parent_org_id == absorbed.id and child_b.parent_org_id == absorbed.id
    assert org_scope(session, survivor, "all").organizations == 1


def test_parent_link_is_inherited_when_the_survivor_has_none_and_restored(session: Session) -> None:
    group = make_org(session, "Big Group")
    survivor = make_org(session, "Widget Energy LLC")
    absorbed = make_org(session, "WIDGET ENERGY", parent=group)
    absorbed.parent_as_of = dt.date(2019, 3, 11)
    session.flush()
    snapshot = merge_mod.serialize_row(absorbed)

    event = merge_mod.merge_organization(session, canonical=survivor, absorbed=absorbed, rationale="r")
    assert "parent_org_id" in event.changed_keys
    assert survivor.parent_org_id == group.id
    assert survivor.parent_as_of == dt.date(2019, 3, 11)
    assert org_scope(session, group, "all").ids == [group.id, survivor.id]

    merge_mod.unmerge_organization(session, event.id)
    assert survivor.parent_org_id is None and survivor.parent_as_of is None
    assert merge_mod.serialize_row(absorbed) == snapshot


def test_merging_a_parent_into_its_own_child_clears_the_self_loop_and_restores_it(session: Session) -> None:
    absorbed = make_org(session, "Loop Energy Holdings")
    survivor = make_org(session, "Loop Energy Holdings LLC", parent=absorbed)
    event = merge_mod.merge_organization(session, canonical=survivor, absorbed=absorbed, rationale="r")
    assert survivor.parent_org_id is None and survivor.parent_source_id is None
    assert event.before is not None and event.before["absorbed"]["child_organization_ids"] == []
    merge_mod.unmerge_organization(session, event.id)
    assert survivor.parent_org_id == absorbed.id
    assert survivor.parent_source_id == "curated.test.parents"


def test_a_self_loop_takes_the_next_link_up_the_chain(session: Session) -> None:
    group = make_org(session, "Loop Group")
    absorbed = make_org(session, "Loop Energy Holdings", parent=group)
    survivor = make_org(session, "Loop Energy Holdings LLC", parent=absorbed)
    event = merge_mod.merge_organization(session, canonical=survivor, absorbed=absorbed, rationale="r")
    assert survivor.parent_org_id == group.id
    assert org_scope(session, group, "all").ids == [group.id, survivor.id]
    merge_mod.unmerge_organization(session, event.id)
    assert survivor.parent_org_id == absorbed.id


# ------------------------------------------------------------------------------- the collision case
def test_collision_keeps_both_rows_and_unmerge_is_exact(session: Session) -> None:
    """Same asset, same role, same source on both rows: `uq_asset_owner_edge` forbids moving it, so
    it stays on the redirect, recorded against the survivor row it duplicates. Same asset and role
    from a different source is not a collision: it moves, and the survivor carries both."""
    eia = make_source(session, "src.eia")
    gem = make_source(session, "src.gem")
    survivor = make_org(session, "MarkWest Liberty Midstream & Resources")
    absorbed = make_org(session, "Markwest Liberty Midstream & Res")
    plant = make_asset(session, eia, "MOBLEY", asset_type="gas_processing_plant")
    other = make_asset(session, eia, "SHERWOOD", asset_type="gas_processing_plant")
    kept = make_edge(session, plant, survivor, eia, role="owner", share_pct=60.0)
    clash = make_edge(session, plant, absorbed, eia, role="owner", share_pct=40.0)
    second_source = make_edge(session, plant, absorbed, gem, role="owner")
    moving = make_edge(session, other, absorbed, eia, role="owner")
    alias_kept = make_alias(session, survivor, eia, "Markwest Liberty Midstream & Res")
    alias_clash = make_alias(session, absorbed, eia, "Markwest Liberty Midstream & Res")
    edge_rows_before = {
        e.id: (e.organization_id, e.asset_id, e.role, e.source_id)
        for e in session.scalars(select(AssetOwner))
    }

    event = merge_mod.merge_organization(session, canonical=survivor, absorbed=absorbed, rationale="10-K")

    assert event.before is not None
    absorbed_payload = event.before["absorbed"]
    assert absorbed_payload["asset_owner_collisions"] == [
        {"id": str(clash.id), "survivor_edge_id": str(kept.id)}
    ]
    assert sorted(absorbed_payload["asset_owner_ids"]) == sorted([str(second_source.id), str(moving.id)])
    assert absorbed_payload["organization_alias_collisions"] == [
        {"id": str(alias_clash.id), "survivor_alias_id": str(alias_kept.id)}
    ]
    for row in (clash, second_source, moving, alias_clash):
        session.refresh(row)
    assert clash.organization_id == absorbed.id  # kept, not collapsed, not deleted
    assert float(clash.share_pct) == 40.0
    assert second_source.organization_id == survivor.id
    assert moving.organization_id == survivor.id
    assert alias_clash.organization_id == absorbed.id
    assert session.scalar(select(AssetOwner).where(AssetOwner.id == clash.id)) is not None
    # no spelling alias was written: the survivor already carries that spelling
    assert "written_alias_id" not in (event.after or {})
    # the survivor's page still counts each asset once
    assert organization_asset_totals(session, survivor, scope="self")["assets"] == 2

    merge_mod.unmerge_organization(session, event.id)
    for row in (second_source, moving, alias_clash):
        session.refresh(row)
    edge_rows_after = {
        e.id: (e.organization_id, e.asset_id, e.role, e.source_id)
        for e in session.scalars(select(AssetOwner))
    }
    assert edge_rows_after == edge_rows_before
    assert alias_clash.organization_id == absorbed.id


# ------------------------------------------------------------------ an event in the old shape
def test_an_old_shape_event_still_unmerges(session: Session) -> None:
    """Events written before 2026-09-26 carry `sponsored_proposal_ids` and nothing else under
    `before.absorbed`. Unmerge must read the missing keys as empty, restore the row, and leave
    everything the old merge never moved exactly where it is."""
    src = make_source(session, "src.old")
    survivor = make_org(session, "Old Shape Power LLC")
    absorbed = make_org(session, "OLD SHAPE POWER")
    asset = make_asset(session, src, "O1", asset_type="power_plant")
    stranded = make_edge(session, asset, absorbed, src, role="owner")
    snapshot = merge_mod.serialize_row(absorbed)
    last_changed = survivor.last_changed

    # What the pre-2026-09-26 `merge_organization` wrote for an edge-only organisation.
    absorbed.merged_into_id = survivor.id
    old_event = Event(
        subject_type="organization",
        subject_id=survivor.id,
        event_type="merged",
        observed_at=RETRIEVED,
        before={
            "surviving": {"last_changed": last_changed.isoformat()},
            "absorbed": {
                "id": str(absorbed.id),
                "public_id": absorbed.public_id,
                "slug": absorbed.slug,
                "entity": snapshot,
                "sponsored_proposal_ids": [],
            },
        },
        after={"surviving": {"last_changed": last_changed.isoformat()}},
        changed_keys=["last_changed"],
        actor_type="pipeline",
        confidence=1.0,
        reason="old shape",
        idempotency_key=f"merge:organization:{survivor.id}:{absorbed.id}",
    )
    session.add(old_event)
    session.flush()

    undo = merge_mod.unmerge_organization(session, old_event.id)
    assert undo.reverses_event_id == old_event.id
    assert merge_mod.serialize_row(absorbed) == snapshot
    session.refresh(stranded)
    assert stranded.organization_id == absorbed.id
    # and a second call is the same event, not a second reversal
    assert merge_mod.unmerge_organization(session, old_event.id).id == undo.id


# ------------------------------------------------------------------------- the curated merge file
def _write_rules(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    path = tmp_path / "merges.yaml"
    path.write_text(body, encoding="utf-8")
    return path


_RULE = (
    "  - absorb: TALLGRASS INTERSTATE GAS TRANSMISSIO\n"
    "    into: Tallgrass Interstate Gas Transmission\n"
    "    rationale: >-\n"
    "      10-K names TIGT and its Huntsman storage field.\n"
    "    source_url: https://www.sec.gov/Archives/edgar/data/1633651/000163365119000009/tge2018123110k.htm\n"
    "    retrieved_at: '2026-09-26T23:12:45Z'\n"
)


def test_the_repo_merge_file_parses_and_cites_every_rule() -> None:
    rules = read_merge_rules()
    assert len(rules) == 3
    assert all(r.source_url.startswith("https://") and r.rationale for r in rules)
    assert {r.into for r in rules} == {
        "Tallgrass Interstate Gas Transmission",
        "MarkWest Liberty Midstream & Resources",
        "Green Knight Economic Development Corporation",
    }


def test_load_merges_applies_once_and_reports_the_rest(
    session: Session, tallgrass: dict[str, object], tmp_path: pathlib.Path
) -> None:
    survivor, absorbed = tallgrass["survivor"], tallgrass["absorbed"]
    assert isinstance(survivor, Organization) and isinstance(absorbed, Organization)
    make_org(session, "Twin Name Co")
    make_org(session, "Twin Name Co")
    path = _write_rules(
        tmp_path,
        "merges:\n" + _RULE + "  - absorb: Nobody Loaded LLC\n    into: Tallgrass Energy\n    rationale: x\n"
        "    source_url: https://example.org/a\n    retrieved_at: '2026-09-26T00:00:00Z'\n"
        "  - absorb: Twin Name Co\n    into: Tallgrass Energy\n    rationale: x\n"
        "    source_url: https://example.org/b\n    retrieved_at: '2026-09-26T00:00:00Z'\n"
        "  - absorb: tallgrass energy\n    into: Tallgrass Energy\n    rationale: x\n"
        "    source_url: https://example.org/c\n    retrieved_at: '2026-09-26T00:00:00Z'\n",
    )

    first = load_merges(session, path)
    assert first.merged == ["TALLGRASS INTERSTATE GAS TRANSMISSIO -> Tallgrass Interstate Gas Transmission"]
    assert first.missing == ["Nobody Loaded LLC -> Tallgrass Energy"]
    assert len(first.conflicts) == 2
    assert "more than one" in first.conflicts[0] and "one organisation row" in first.conflicts[1]
    session.refresh(absorbed)
    assert absorbed.merged_into_id == survivor.id
    event = session.scalar(select(Event).where(Event.subject_id == survivor.id, Event.event_type == "merged"))
    assert event is not None
    assert event.actor_type == "user"
    assert event.source_url is not None and event.source_url.endswith("tge2018123110k.htm")
    assert event.retrieved_at is not None
    assert event.reason is not None and "Huntsman" in event.reason

    second = load_merges(session, path)
    assert second.merged == [] and len(second.already_merged) == 1
    assert second.as_report()["already_merged"] == first.as_report()["merged"]
    merged_events = session.scalars(select(Event).where(Event.event_type == "merged")).all()
    assert len(merged_events) == 1


def test_load_merges_refuses_a_row_already_merged_elsewhere(session: Session, tmp_path: pathlib.Path) -> None:
    survivor = make_org(session, "Tallgrass Interstate Gas Transmission")
    absorbed = make_org(session, "TALLGRASS INTERSTATE GAS TRANSMISSIO")
    elsewhere = make_org(session, "Someone Else LLC")
    absorbed.merged_into_id = elsewhere.id
    session.flush()
    result = load_merges(session, _write_rules(tmp_path, "merges:\n" + _RULE))
    assert result.merged == [] and "already merged into another" in result.conflicts[0]

    absorbed.merged_into_id = None
    survivor.merged_into_id = elsewhere.id
    session.flush()
    result = load_merges(session, _write_rules(tmp_path, "merges:\n" + _RULE))
    assert result.merged == [] and "itself merged" in result.conflicts[0]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("merges: 3\n", "top-level `merges:` list"),
        ("merges:\n  - just a string\n", "not a mapping"),
        ("merges:\n  - absorb: A\n    into: B\n", "lacks"),
    ],
)
def test_a_malformed_merge_file_is_a_value_error(tmp_path: pathlib.Path, body: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        read_merge_rules(_write_rules(tmp_path, body))


def test_load_merges_event_carries_the_registered_source(
    session: Session, tallgrass: dict[str, object], tmp_path: pathlib.Path
) -> None:
    """docs/22 A-22-23 closed (2026-09-27): `curated.organization_merges` is registered in
    `data/sources.yaml` and the docs/13 §6 matrix, so a curated merge event carries a full provenance
    quartet instead of a NULL `source_id`."""
    survivor = tallgrass["survivor"]
    assert isinstance(survivor, Organization)
    load_merges(session, _write_rules(tmp_path, "merges:\n" + _RULE))
    event = session.scalar(select(Event).where(Event.subject_id == survivor.id, Event.event_type == "merged"))
    assert event is not None
    assert event.source_id == "curated.organization_merges"
    assert event.licence_id is not None and event.licence_id.startswith("curated.organization_merges#")
    assert event.source is not None and event.source.url.endswith("organizations/merges.yaml")
    assert event.licence is not None and event.licence.reuse_class == "open"
