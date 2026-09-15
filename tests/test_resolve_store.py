"""services/resolve/merge.py: canonical-record choice, the confidence gate (including refusal on
the ISO-NE id-reuse case), merge event shape, and organization resolution.

`tests/test_resolve_store_unmerge.py` covers the unmerge round trip.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import SourceEntry
from pipeline.normalize import norm_org
from services.db.models import Organization, Proposal, ProposalSource, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id, slugify
from services.ingest.loader import upsert_licence_and_source
from services.resolve import merge as merge_mod
from services.resolve.merge import ClusterEdge, ClusterMember
from services.resolve.models import ResolutionDecision

UTC = dt.UTC


@pytest.fixture()
def session() -> Session:
    import services.resolve.models  # noqa: F401 -- register resolution_decision on Base.metadata

    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def make_source(session: Session, id_: str, *, reuse: str = "open") -> Source:
    entry = SourceEntry.from_yaml(
        {
            "id": id_,
            "name": f"Test {id_}",
            "jurisdiction": "US-TX",
            "category": "generation_queue",
            "operator": "Test ISO",
            "url": f"https://example.org/{id_}",
            "access": "bulk_file",
            "reuse": reuse,
            "cadence": "weekly",
            "tier": 1,
            "license": f"licence for {id_}",
        }
    )
    return upsert_licence_and_source(session, entry, "test-manifest")


def make_proposal(
    session: Session,
    source: Source,
    *,
    source_record_id: str,
    name: str = "Test Project",
    queue_id: str | None = None,
    eia_plant_id: str | None = None,
    retrieved_at: dt.datetime = dt.datetime(2026, 1, 1, tzinfo=UTC),
    sponsor: Organization | None = None,
) -> tuple[Proposal, ProposalSource]:
    identifiers: dict[str, object] = {}
    if queue_id:
        identifiers["queue_ids"] = [{"iso": "TEST", "id": queue_id}]
    if eia_plant_id:
        identifiers["eia_plant_id"] = eia_plant_id
    proposal = Proposal(
        public_id="",
        slug="",
        kind="storage",
        name_canonical=name,
        jurisdiction="US-TX",
        lifecycle_state="filed",
        identifiers=identifiers,
        publish_state="public",
        min_reuse_class=source.licence.reuse_class,
        source_count=1,
        sponsor_org_id=sponsor.id if sponsor else None,
    )
    session.add(proposal)
    session.flush()
    proposal.public_id = public_id("prop", proposal.id)
    proposal.slug = f"{slugify(name)}-{proposal.public_id[-6:].lower()}"
    session.flush()

    link = ProposalSource(
        proposal_id=proposal.id,
        source_id=source.id,
        source_record_id=source_record_id,
        source_url=f"{source.url}/{source_record_id}",
        retrieved_at=retrieved_at,
        licence_id=source.licence_id,
        raw={},
        normalised={},
        first_seen=retrieved_at,
        last_seen=retrieved_at,
    )
    session.add(link)
    session.flush()
    return proposal, link


def make_organization(session: Session, name: str) -> Organization:
    org = Organization(
        public_id="", slug="", name_canonical=name, name_normalised=name.lower(), type="other", country="US"
    )
    session.add(org)
    session.flush()
    org.public_id = public_id("org", org.id)
    org.slug = f"{slugify(name)}-{org.public_id[-6:].lower()}"
    session.flush()
    return org


# ---------------------------------------------------------------------------------- canonical choice
def test_choose_canonical_prefers_eia_id(session: Session) -> None:
    src = make_source(session, "src.a")
    p1, _ = make_proposal(session, src, source_record_id="Q1", queue_id="Q1")
    p2, _ = make_proposal(session, src, source_record_id="E1", eia_plant_id="12345")

    members = [
        ClusterMember(
            p1.id, src.id, "Q1", has_eia_id=False, retrieved_at=dt.datetime(2026, 6, 1, tzinfo=UTC)
        ),
        ClusterMember(p2.id, src.id, None, has_eia_id=True, retrieved_at=dt.datetime(2026, 1, 1, tzinfo=UTC)),
    ]
    winner = merge_mod.choose_canonical(members)
    assert winner.proposal_id == p2.id  # EIA id wins even though it was retrieved earlier


def test_choose_canonical_prefers_most_recent_when_no_eia_id(session: Session) -> None:
    src = make_source(session, "src.b")
    p1, _ = make_proposal(session, src, source_record_id="Q1")
    p2, _ = make_proposal(session, src, source_record_id="Q2")

    members = [
        ClusterMember(
            p1.id, src.id, "Q1", has_eia_id=False, retrieved_at=dt.datetime(2026, 1, 1, tzinfo=UTC)
        ),
        ClusterMember(
            p2.id, src.id, "Q2", has_eia_id=False, retrieved_at=dt.datetime(2026, 6, 1, tzinfo=UTC)
        ),
    ]
    winner = merge_mod.choose_canonical(members)
    assert winner.proposal_id == p2.id


def test_choose_canonical_tiebreak_lowest_source_id(session: Session) -> None:
    src_a = make_source(session, "aaa.source")
    src_z = make_source(session, "zzz.source")
    same_time = dt.datetime(2026, 1, 1, tzinfo=UTC)
    pa, _ = make_proposal(session, src_a, source_record_id="Q1")
    pz, _ = make_proposal(session, src_z, source_record_id="Q1")
    members = [
        ClusterMember(pz.id, src_z.id, "Q1", has_eia_id=False, retrieved_at=same_time),
        ClusterMember(pa.id, src_a.id, "Q1", has_eia_id=False, retrieved_at=same_time),
    ]
    winner = merge_mod.choose_canonical(members)
    assert winner.proposal_id == pa.id  # "aaa.source" sorts before "zzz.source"


# --------------------------------------------------------------------------------------- the gate
def test_id_reuse_conflict_detects_same_source_same_queue_id_shared_by_two_records() -> None:
    """The measured ISO-NE bug (docs/22 §7.1: `isone:84` / `isone:84#5`, "Phase 3" vs "Phase I",
    the same reused queue id covering two different projects) and docs/22 §5's "within-source
    duplicate pairs" (85 ISO-NE, 2 NYISO)."""
    import uuid as _uuid

    same_time = dt.datetime(2026, 1, 1, tzinfo=UTC)
    members = [
        ClusterMember(
            _uuid.uuid4(), "us.iso.isone.gen_queue", "84", has_eia_id=False, retrieved_at=same_time
        ),
        ClusterMember(
            _uuid.uuid4(), "us.iso.isone.gen_queue", "84", has_eia_id=False, retrieved_at=same_time
        ),
    ]
    conflict, detail = merge_mod.id_reuse_conflict(members)
    assert conflict is True
    assert "84" in (detail or "")

    allowed, reason = merge_mod.gate_cluster(members, min_score=100.0)
    assert allowed is False
    assert "id-reuse guard" in reason


def test_id_reuse_conflict_allows_same_source_different_queue_ids() -> None:
    """Documented departure from the task's literal wording (module docstring in merge.py):
    two DIFFERENT queue ids from the same source, both legitimately in one cluster (e.g. two real
    co-located interconnection requests bridged by one EIA plant), is not itself id reuse and
    must not be refused -- refusing it would gut recall on docs/22 §7.4's granularity-mismatch
    complexes (measured: 73 of 281 loadable clusters would trip a naive "different ids" reading)."""
    import uuid as _uuid

    same_time = dt.datetime(2026, 1, 1, tzinfo=UTC)
    members = [
        ClusterMember(
            _uuid.uuid4(), "us.iso.caiso.gen_queue", "1479", has_eia_id=False, retrieved_at=same_time
        ),
        ClusterMember(
            _uuid.uuid4(), "us.iso.caiso.gen_queue", "1480", has_eia_id=False, retrieved_at=same_time
        ),
    ]
    conflict, _ = merge_mod.id_reuse_conflict(members)
    assert conflict is False


def test_gate_cluster_refuses_singleton() -> None:
    import uuid as _uuid

    members = [ClusterMember(_uuid.uuid4(), "src", "Q1", False, dt.datetime(2026, 1, 1, tzinfo=UTC))]
    allowed, reason = merge_mod.gate_cluster(members, min_score=100.0)
    assert allowed is False
    assert "singleton" in reason


def test_gate_cluster_refuses_below_threshold() -> None:
    import uuid as _uuid

    same_time = dt.datetime(2026, 1, 1, tzinfo=UTC)
    members = [
        ClusterMember(_uuid.uuid4(), "src", "Q1", False, same_time),
        ClusterMember(_uuid.uuid4(), "src2", "Q2", False, same_time),
    ]
    allowed, reason = merge_mod.gate_cluster(members, min_score=74.9)
    assert allowed is False
    assert "below threshold" in reason


def test_gate_cluster_accepts_valid_cluster() -> None:
    import uuid as _uuid

    same_time = dt.datetime(2026, 1, 1, tzinfo=UTC)
    members = [
        ClusterMember(_uuid.uuid4(), "src", "Q1", False, same_time),
        ClusterMember(_uuid.uuid4(), "src2", "Q2", False, same_time),
    ]
    allowed, reason = merge_mod.gate_cluster(members, min_score=75.0)
    assert allowed is True
    assert reason


# --------------------------------------------------------------------------------------- merge event
def test_merge_proposal_event_shape(session: Session) -> None:
    src = make_source(session, "src.c")
    canonical, canonical_link = make_proposal(session, src, source_record_id="Q1", name="Canonical Project")
    absorbed, absorbed_link = make_proposal(session, src, source_record_id="Q2", name="Absorbed Project")

    event = merge_mod.merge_proposal(
        session, canonical=canonical, absorbed=absorbed, score=88.5, rationale="test merge"
    )

    assert event.event_type == "merged"
    assert event.subject_type == "proposal"
    assert event.subject_id == canonical.id
    assert event.actor_type == "model"
    assert event.confidence == pytest.approx(0.885)

    # invariant M1 (docs/21 §6.3): enough state in `before` to reverse without reading any other row
    assert event.before is not None
    assert event.before["absorbed"]["id"] == str(absorbed.id)
    assert event.before["absorbed"]["entity"]["merged_into_id"] is None
    assert event.before["absorbed"]["entity"]["publish_state"] == "public"
    assert event.before["absorbed"]["proposal_source_ids"] == [str(absorbed_link.id)]
    assert event.before["surviving"]["source_count"] == 1

    # the mutation actually applied
    assert absorbed.merged_into_id == canonical.id
    assert absorbed.publish_state == "unpublished"
    assert canonical.source_count == 2
    session.refresh(absorbed_link)
    assert absorbed_link.proposal_id == canonical.id  # re-pointed
    session.refresh(canonical_link)
    assert canonical_link.proposal_id == canonical.id  # untouched

    # never deleted
    assert session.get(Proposal, absorbed.id) is not None

    # idempotent
    event2 = merge_mod.merge_proposal(
        session, canonical=canonical, absorbed=absorbed, score=88.5, rationale="test merge"
    )
    assert event2.id == event.id
    assert canonical.source_count == 2  # not double-applied


def test_merge_proposal_refuses_self_merge(session: Session) -> None:
    src = make_source(session, "src.d")
    p, _ = make_proposal(session, src, source_record_id="Q1")
    with pytest.raises(ValueError, match="itself"):
        merge_mod.merge_proposal(session, canonical=p, absorbed=p, score=100.0, rationale="x")


# ------------------------------------------------------------------------------------ apply_cluster
def test_apply_cluster_merges_when_gate_passes(session: Session) -> None:
    src = make_source(session, "src.e")
    p1, _ = make_proposal(session, src, source_record_id="Q1")
    p2, _ = make_proposal(session, src, source_record_id="Q2")
    same_time = dt.datetime(2026, 1, 1, tzinfo=UTC)
    members = [
        ClusterMember(p1.id, src.id, "Q1", False, same_time),
        ClusterMember(p2.id, src.id, "Q2", False, same_time),
    ]
    edges = [ClusterEdge(p1.id, p2.id, 90.0, "name=95; county=100")]

    result = merge_mod.apply_cluster(session, members, edges, cluster_key="1")
    assert result.action == "merged"
    assert result.members_merged == 1
    session.refresh(p2)
    assert p2.merged_into_id == p1.id  # p1 wins: lower source_record_id tiebreak at equal time


def test_apply_cluster_files_resolution_decision_below_threshold(session: Session) -> None:
    src = make_source(session, "src.f")
    p1, _ = make_proposal(session, src, source_record_id="Q1")
    p2, _ = make_proposal(session, src, source_record_id="Q2")
    same_time = dt.datetime(2026, 1, 1, tzinfo=UTC)
    members = [
        ClusterMember(p1.id, src.id, "Q1", False, same_time),
        ClusterMember(p2.id, src.id, "Q2", False, same_time),
    ]
    edges = [ClusterEdge(p1.id, p2.id, 60.0, "weak name match")]

    result = merge_mod.apply_cluster(session, members, edges, cluster_key="2")
    assert result.action == "proposed"
    assert result.members_merged == 0
    session.refresh(p1)
    session.refresh(p2)
    assert p1.merged_into_id is None
    assert p2.merged_into_id is None

    decisions = session.scalars(select(ResolutionDecision)).all()
    assert len(decisions) == 1
    assert decisions[0].status == "proposed"
    assert decisions[0].cluster_key == "2"
    assert "below threshold" in decisions[0].gate_reason


def test_apply_cluster_files_resolution_decision_on_id_reuse(session: Session) -> None:
    # ISO-NE itself is `restricted` and never reaches the store at all (services/ingest/loader.py
    # refuses it outright, proven in services/resolve/report.py's build_store); this uses an
    # ISO-NE-shaped id only as a label to exercise the gate function in isolation, on a source
    # this fixture registers as `open` purely to satisfy the FK.
    src = make_source(session, "us.iso.isone.gen_queue")
    p1, _ = make_proposal(session, src, source_record_id="84")
    p2, _ = make_proposal(session, src, source_record_id="84#2")
    same_time = dt.datetime(2026, 1, 1, tzinfo=UTC)
    members = [
        ClusterMember(p1.id, src.id, "84", False, same_time),
        ClusterMember(p2.id, src.id, "84", False, same_time),
    ]
    edges = [ClusterEdge(p1.id, p2.id, 95.0, "fuzzy name match, same reused queue id")]

    result = merge_mod.apply_cluster(session, members, edges, cluster_key="3")
    assert result.action == "proposed"
    assert "id-reuse guard" in result.gate_reason


# -------------------------------------------------------------------------------- organization merge
def test_merge_organization_creates_alias_and_repoints_sponsor(session: Session) -> None:
    src = make_source(session, "src.g")
    canonical = make_organization(session, "Acme Power LLC")
    absorbed = make_organization(session, "ACME POWER, LLC")
    proposal, _link = make_proposal(session, src, source_record_id="Q1", sponsor=absorbed)

    event = merge_mod.merge_organization(
        session, canonical=canonical, absorbed=absorbed, rationale="normalised match"
    )
    assert event.event_type == "merged"
    assert event.subject_type == "organization"
    assert absorbed.merged_into_id == canonical.id
    session.refresh(proposal)
    assert proposal.sponsor_org_id == canonical.id  # re-pointed

    from services.db.models import OrganizationAlias

    alias = session.scalar(select(OrganizationAlias).where(OrganizationAlias.organization_id == canonical.id))
    assert alias is not None
    assert alias.alias == "ACME POWER, LLC"
    assert alias.kind == "filing_spelling"
    assert alias.source_id == src.id


def test_resolve_organizations_merges_normalised_groups(session: Session) -> None:
    src = make_source(session, "src.h")
    org_a = make_organization(session, "Acme Power LLC")
    org_b = make_organization(session, "Acme Power Corp")
    org_c = make_organization(session, "Unrelated Co")
    make_proposal(session, src, source_record_id="Q1", sponsor=org_a)
    make_proposal(session, src, source_record_id="Q2", sponsor=org_a)
    make_proposal(session, src, source_record_id="Q3", sponsor=org_b)
    make_proposal(session, src, source_record_id="Q4", sponsor=org_c)

    report = merge_mod.resolve_organizations(session, norm_org)

    assert report.groups_considered == 1  # "ACME POWER" (suffix-stripped); "UNRELATED CO" is alone
    assert report.groups_merged == 1
    assert report.organizations_absorbed == 1
    session.refresh(org_a)
    session.refresh(org_b)
    assert org_a.merged_into_id is None  # org_a sponsors 2 proposals vs org_b's 1: org_a wins
    assert org_b.merged_into_id == org_a.id
