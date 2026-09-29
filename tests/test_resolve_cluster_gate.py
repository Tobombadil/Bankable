"""services/resolve: the whole-cluster coherence check (rule K) and the loaded-only cluster builder
with rollup projection (rule L) of docs/22 §22 (lane H5).

Member MW and technologies are copied from the dev-store clusters of 2026-09-29.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import SourceEntry
from services.db.models import Proposal, ProposalSource, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id, slugify
from services.ingest.loader import upsert_licence_and_source
from services.resolve import merge as merge_mod
from services.resolve.merge import ClusterEdge, ClusterMember
from services.resolve.models import ResolutionDecision
from services.resolve.report import build_clusters

UTC = dt.UTC
T0 = dt.datetime(2026, 9, 29, tzinfo=UTC)
EIA, NYISO, ERCOT, CAISO = (
    "us.eia.860m",
    "us.iso.nyiso.gen_queue",
    "us.iso.ercot.gen_queue",
    "us.iso.caiso.gen_queue",
)


def member(
    source: str, mw: float | None, tech: str | None, *, plant: str | None = None, queue: str | None = None
) -> ClusterMember:
    return ClusterMember(
        proposal_id=uuid.uuid4(),
        source_id=source,
        queue_id=queue,
        has_eia_id=plant is not None,
        retrieved_at=T0,
        capacity_mw=mw,
        technology=tech,
        eia_plant_id=plant,
    )


# ------------------------------------------------------------------ rule K: coherence
def test_town_name_chain_onto_a_small_plant_is_incoherent() -> None:
    # Riverhead: three NYISO solar requests (24, 20, 7.5 MW) chained onto a 4.9 + 4.0 MW EIA plant.
    members = [
        member(NYISO, 24.0, "solar", queue="0442"),
        member(NYISO, 20.0, "solar", queue="0477"),
        member(NYISO, 7.5, "solar", queue="0603"),
        member(EIA, 4.9, "solar", plant="70242"),
        member(EIA, 4.0, "storage", plant="70242"),
    ]
    conflict, detail = merge_mod.coherence_conflict(members)
    assert conflict
    assert detail is not None and "51.5 MW solar" in detail
    allowed, reason = merge_mod.gate_cluster(members, 90.0)
    assert not allowed and reason.startswith("coherence check")


@pytest.mark.parametrize(
    "members",
    [
        # hybrid split across two ERCOT requests and two EIA generators (Harryoung)
        [
            member(ERCOT, 190.01, "solar", queue="25INR0270"),
            member(ERCOT, 190.01, "storage", queue="25INR0552"),
            member(EIA, 190.0, "solar", plant="67777"),
            member(EIA, 190.0, "storage", plant="67777"),
        ],
        # phases that add up to the plant (Vast Sands Power I + II, two 440 MW turbines)
        [
            member(ERCOT, 440.0, "gas_ct", queue="28INR0105"),
            member(ERCOT, 440.0, "gas_ct", queue="28INR0109"),
            member(EIA, 440.0, "gas_ct", plant="69689"),
            member(EIA, 440.0, "gas_ct", plant="69689"),
        ],
        # EIA lists only the storage half of a hybrid whose solar already runs (Duffy)
        [
            member(ERCOT, 502.46, "solar", queue="23INR0057"),
            member(ERCOT, 241.05, "storage", queue="26INR0250"),
            member(EIA, 235.9, "storage", plant="69295"),
        ],
        # NYISO lists one project under its cluster id and its queue position (KCE NY 30)
        [
            member(NYISO, 50.0, "storage", queue="C24-008"),
            member(NYISO, 50.0, "storage", queue="1448"),
            member(EIA, 50.0, "storage", plant="67838"),
        ],
        # a hybrid request counts once, in its largest EIA family (Gonzaga, x1.36)
        [
            member(CAISO, 76.35, "wind", queue="1378"),
            member(CAISO, 103.65, "wind_storage", queue="1718"),
            member(EIA, 51.8, "storage", plant="68980"),
            member(EIA, 40.3, "wind", plant="68981"),
            member(EIA, 34.6, "wind", plant="68981"),
            member(EIA, 57.9, "wind", plant="68981"),
        ],
        # a single request is never judged here (rule C covers it pairwise)
        [member(CAISO, 1150.0, "solar_storage", queue="1949"), member(EIA, 342.7, "solar", plant="69661")],
    ],
)
def test_legitimate_clusters_are_coherent(members: list[ClusterMember]) -> None:
    assert merge_mod.coherence_conflict(members) == (False, None)


def test_one_request_per_family_is_left_to_rule_c() -> None:
    # Briggs (K-v1): one ERCOT solar and one ERCOT storage request against an EIA plant of 305 MW
    # solar and 70.5 MW storage. The storage request is 4.77x the plant's storage, but it is a single
    # request in its family, so the whole-cluster check does not judge it and the cluster merges.
    members = [
        member(ERCOT, 323.7, "solar", queue="23INR0059"),
        member(ERCOT, 336.0, "storage", queue="24INR0058"),
        member(EIA, 305.0, "solar", plant="66691"),
        member(EIA, 70.5, "storage", plant="66691"),
    ]
    assert merge_mod.coherence_ratio(members) == (0.0, None)
    assert merge_mod.gate_cluster(members, 84.0)[0]


def test_town_name_chain_is_still_reviewed_beside_a_single_storage_request() -> None:
    # Riverhead after K-v1: the three solar requests are one family with >= 2 requests and are still
    # judged, even though the cluster also holds a single storage request that is not.
    members = [
        member(NYISO, 24.0, "solar", queue="0442"),
        member(NYISO, 20.0, "solar", queue="0477"),
        member(NYISO, 7.5, "solar", queue="0603"),
        member(NYISO, 3.0, "storage", queue="9999"),
        member(EIA, 4.9, "solar", plant="70242"),
        member(EIA, 4.0, "storage", plant="70242"),
    ]
    allowed, reason = merge_mod.gate_cluster(members, 77.0)
    assert not allowed
    assert "3 requests total 51.5 MW solar against 4.9 MW" in reason


def test_members_without_capacity_are_not_judged() -> None:
    members = [
        member(NYISO, None, None, queue="1"),
        member(NYISO, None, None, queue="2"),
        member(EIA, 5.0, "solar", plant="1"),
    ]
    assert merge_mod.coherence_ratio(members) == (0.0, None)


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
            "jurisdiction": "US-NY",
            "category": "generation_queue",
            "operator": "Test",
            "url": f"https://example.org/{id_}",
            "access": "bulk_file",
            "reuse": "open",
            "cadence": "weekly",
            "tier": 1,
            "license": f"licence for {id_}",
        }
    )
    return upsert_licence_and_source(session, entry, "test-manifest")


def make_proposal(session: Session, source: Source, record_id: str) -> Proposal:
    proposal = Proposal(
        public_id="",
        slug="",
        kind="generation",
        name_canonical=f"Riverhead {record_id}",
        jurisdiction="US-NY",
        lifecycle_state="filed",
        identifiers={},
        publish_state="public",
        min_reuse_class=source.licence.reuse_class,
        source_count=1,
    )
    session.add(proposal)
    session.flush()
    proposal.public_id = public_id("prop", proposal.id)
    proposal.slug = f"{slugify(proposal.name_canonical)}-{proposal.public_id[-6:].lower()}"
    session.add(
        ProposalSource(
            proposal_id=proposal.id,
            source_id=source.id,
            source_record_id=record_id,
            source_url=f"{source.url}/{record_id}",
            retrieved_at=T0,
            licence_id=source.licence_id,
            raw={},
            normalised={},
            first_seen=T0,
            last_seen=T0,
        )
    )
    session.flush()
    return proposal


def test_incoherent_cluster_goes_to_review_not_merge(session: Session) -> None:
    nyiso, eia = make_source(session, NYISO), make_source(session, EIA)
    a, b, c = (make_proposal(session, nyiso, r) for r in ("0442", "0477", "0603"))
    plant = make_proposal(session, eia, "70242-215")
    members = [
        ClusterMember(a.id, NYISO, "0442", False, T0, 24.0, "solar"),
        ClusterMember(b.id, NYISO, "0477", False, T0, 20.0, "solar"),
        ClusterMember(c.id, NYISO, "0603", False, T0, 7.5, "solar"),
        ClusterMember(plant.id, EIA, None, True, T0, 4.9, "solar", "70242"),
    ]
    edges = [ClusterEdge(p.id, plant.id, 77.0, "name=91; county=100") for p in (a, b, c)]
    result = merge_mod.apply_cluster(session, members, edges, cluster_key="6756.0")
    assert result.action == "proposed"
    assert result.members_merged == 0
    assert all(p.merged_into_id is None for p in (a, b, c, plant))
    decisions = session.scalars(select(ResolutionDecision)).all()
    assert len(decisions) == 3
    assert all(d.gate_reason.startswith("coherence check") for d in decisions)


# ------------------------------------------------------------------ rule L: loaded-only clusters
def _frames(
    rows: list[dict[str, object]],
    edges: list[tuple[int, int, float]],
    rollups: list[dict[str, object]] | None = None,
):
    df = pd.DataFrame(rows)
    df["retrieved_at"] = "2026-09-29T00:00:00Z"
    extra = pd.DataFrame(rollups or [])
    clusters = pd.concat([df, extra], ignore_index=True)
    clusters["cluster_id"] = 1.0
    matches = pd.DataFrame(
        [
            {"li": a, "ri": b, "score": s, "rationale": "name=95", "accepted": True, "cluster_id": 1.0}
            for a, b, s in edges
        ]
    )
    return df, matches, clusters


def row(source: str, rid: str, **extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "source_id": source,
        "source_record_id": rid,
        "queue_id": None,
        "eia_plant_id": None,
        "capacity_mw": None,
        "technology": None,
    }
    base.update(extra)
    return base


def test_unloaded_record_does_not_bridge_two_loaded_ones() -> None:
    # Bonanza in dev: two EIA generators joined only through a Permitting Dashboard record the dev
    # store never loads.
    df, matches, clusters = _frames(
        [
            row(EIA, "66908-BZPV", eia_plant_id="66908", capacity_mw=300.0, technology="solar"),
            row("us.permits_dashboard", "96071"),
            row(EIA, "66908-BZES", eia_plant_id="66908", capacity_mw=195.0, technology="storage"),
        ],
        [(0, 1, 78.0), (2, 1, 78.0)],
    )
    loaded = {EIA: EIA}
    ids = {(EIA, "66908-BZPV"): uuid.uuid4(), (EIA, "66908-BZES"): uuid.uuid4()}
    members, edges = build_clusters(df, matches, clusters, loaded, ids)
    assert all(len(v) < 2 for v in members.values())
    assert not any(edges.values())


def test_components_of_one_pipeline_cluster_are_split_and_named() -> None:
    df, matches, clusters = _frames(
        [
            row(NYISO, "a"),
            row(NYISO, "b"),
            row("us.permits_dashboard", "x"),
            row(NYISO, "c"),
            row(NYISO, "d"),
        ],
        [(0, 1, 90.0), (1, 2, 80.0), (2, 3, 80.0), (3, 4, 85.0)],
    )
    ids = {(NYISO, r): uuid.uuid4() for r in "abcd"}
    members, edges = build_clusters(df, matches, clusters, {NYISO: NYISO}, ids)
    assert sorted(members) == ["1.0", "1.0#1"]
    assert {m.proposal_id for m in members["1.0"]} == {ids[(NYISO, "a")], ids[(NYISO, "b")]}
    assert {m.proposal_id for m in members["1.0#1"]} == {ids[(NYISO, "c")], ids[(NYISO, "d")]}
    assert [e.score for e in edges["1.0#1"]] == [85.0]


def test_edge_to_a_plant_rollup_is_projected_onto_its_generators() -> None:
    # KEY STORAGE 1 (300.1 MW) matches only the 300 MW plant total of three EIA generators.
    gens = [(f"68461-KEYB{x}", mw) for x, mw in (("A", 75.0), ("B", 190.0), ("C", 35.0))]
    rows = [row(CAISO, "1479", capacity_mw=300.1, technology="storage", queue_id="1479")] + [
        row(EIA, rid, eia_plant_id="68461", capacity_mw=mw, technology="storage") for rid, mw in gens
    ]
    rollup = row(EIA, "plant-68461", eia_plant_id="68461", capacity_mw=300.0, technology="storage")
    df, matches, clusters = _frames(rows, [(4, 0, 86.7)], rollups=[rollup])
    ids = {(CAISO, "1479"): uuid.uuid4(), **{(EIA, rid): uuid.uuid4() for rid, _ in gens}}
    members, edges = build_clusters(df, matches, clusters, {CAISO: CAISO, EIA: EIA}, ids)
    (key,) = members
    assert {m.proposal_id for m in members[key]} == set(ids.values())
    assert len(edges[key]) == 3
    assert all("via EIA plant rollup" in e.rationale for e in edges[key])
    eia_members = [m for m in members[key] if m.source_id == EIA]
    assert sorted(m.capacity_mw or 0 for m in eia_members) == [35.0, 75.0, 190.0]
    assert all(m.eia_plant_id == "68461" and m.technology == "storage" for m in eia_members)
