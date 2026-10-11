"""The site pass over a store (`services/sites/build.py`): grouping, persistence, idempotence, stable
ids through split, merge and retirement, anchors, the review flag and the lifecycle audit."""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from services.db.models import (
    Asset,
    InterconnectionPoint,
    Licence,
    Location,
    Organization,
    Proposal,
    ProposalSource,
    Site,
    SiteAudit,
    SiteMember,
)
from services.db.session import get_sessionmaker
from services.ids import public_id, slugify
from services.sites import build, rules
from tests.db_template import disposing, fresh_engine

UTC = dt.UTC
T0 = dt.datetime(2026, 10, 1, tzinfo=UTC)
T1 = dt.datetime(2026, 10, 2, tzinfo=UTC)
T2 = dt.datetime(2026, 10, 3, tzinfo=UTC)
_seq = itertools.count(1)


@pytest.fixture()
def factory() -> Iterator[sessionmaker[Session]]:
    with disposing(fresh_engine()) as engine:
        yield get_sessionmaker(engine)


@pytest.fixture()
def db(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as s:
        _registry(s)
        s.commit()
        yield s


def _registry(s: Session) -> None:
    s.add(
        Licence(
            id="open",
            name="Open",
            reuse_class="open",
            allows_derived_publication=True,
            allows_raw_publication=True,
            allows_api_redistribution=True,
            allows_bulk_export=True,
            allows_commercial_use=True,
            gate_flag=False,
            evidence_url="https://example.org/terms",
            evidence_retrieved_at=T0,
            classified_by="legal-compliance",
        )
    )
    s.flush()
    from services.db.models import Source

    for sid, category in (("us.iso.caiso.gen_queue", "generation_queue"), ("us.eia.860m", "registry")):
        s.add(
            Source(
                id=sid,
                name=sid,
                category=category,
                jurisdiction="US",
                operator="Test",
                url=f"https://example.org/{sid}",
                access="bulk_file",
                cadence="weekly",
                licence_id="open",
                publish_state="public",
                manifest_version="2026-09-12",
                manifest_hash="0" * 64,
            )
        )
    s.flush()


def org(s: Session, name: str) -> Organization:
    o = Organization(
        public_id="",
        slug="",
        name_canonical=name,
        name_normalised=name.lower(),
        type="developer",
        country="US",
    )
    s.add(o)
    s.flush()
    o.public_id = public_id("org", o.id)
    o.slug = slugify(name) + f"-{next(_seq)}"
    s.flush()
    return o


def point(s: Session, name: str = "Imperial Valley 230kV") -> InterconnectionPoint:
    n = next(_seq)
    pt = InterconnectionPoint(
        public_id=f"poi_{n:010d}",
        operator="CAISO",
        name_display=name,
        name_key=f"k{n}",
        key_rule="t",
        kind="substation",
        source_id="us.iso.caiso.gen_queue",
        source_url="https://example.org/q",
        retrieved_at=T0,
        licence_id="open",
    )
    s.add(pt)
    s.flush()
    return pt


def exact(s: Session, lon: float = -101.569, lat: float = 35.283762) -> Location:
    loc = Location(
        kind="point",
        geom=(lon, lat),
        precision="exact",
        country="US",
        source_id="us.eia.860m",
        source_url="https://example.org/eia",
        retrieved_at=T0,
        licence_id="open",
    )
    s.add(loc)
    s.flush()
    return loc


def prop(
    s: Session,
    name: str,
    *,
    mw: float | None = 100.0,
    state: str = "filed",
    technology: str = "solar",
    sponsor: Organization | None = None,
    poi: InterconnectionPoint | None = None,
    location: Location | None = None,
    record: str | None = None,
    queue_date: str | None = None,
) -> Proposal:
    n = next(_seq)
    p = Proposal(
        public_id="",
        slug="",
        kind="generation",
        name_canonical=name,
        technology=technology,
        capacity_mw=mw,
        jurisdiction="US-CA",
        lifecycle_state=state,
        publish_state="public",
        published_at=T0,
        public_at=T0,
        min_reuse_class="open",
        source_count=1,
        sponsor_org_id=sponsor.id if sponsor else None,
        interconnection_point_id=poi.id if poi else None,
        location_id=location.id if location else None,
    )
    s.add(p)
    s.flush()
    p.public_id = public_id("prop", p.id)
    p.slug = f"p-{n}"
    source_id = "us.eia.860m" if record and "-" in record else "us.iso.caiso.gen_queue"
    s.add(
        ProposalSource(
            proposal_id=p.id,
            source_id=source_id,
            source_record_id=record or f"Q{n}",
            source_url="https://example.org/r",
            retrieved_at=T0,
            licence_id="open",
            raw={},
            normalised={"name_canonical": name, "queue_date": queue_date},
        )
    )
    s.flush()
    return p


def members_of(s: Session, site: Site) -> dict[str, SiteMember]:
    rows = s.scalars(select(SiteMember).where(SiteMember.site_id == site.id)).all()
    return {s.get(Proposal, r.proposal_id).public_id: r for r in rows}  # type: ignore[union-attr]


def live_sites(s: Session) -> list[Site]:
    return list(
        s.scalars(select(Site).where(Site.retired_at.is_(None)).order_by(Site.created_at, Site.public_id))
    )


# ----------------------------------------------------------------------------------- the pass
def test_matador_two_plants_one_site_with_two_levels(db: Session) -> None:
    """Project Matador: nuclear plant 69798 (PMN1-4) and gas plant 69799 at one exact point, two
    Fermi organisations. One site; the lead is the largest nuclear unit; the other nuclear units are
    `unit_of` it; the gas plant's head is `co_located`; its units are `unit_of` the gas head, never
    of the nuclear lead."""
    where = exact(db)
    fermi_n, fermi_a = org(db, "Fermi Nuclear"), org(db, "Fermi America")
    nuclear = [
        prop(
            db,
            "Project Matador Nuclear",
            mw=1117,
            technology="nuclear",
            sponsor=fermi_n,
            location=exact(db),
            record=f"69798-PMN{i}",
        )
        for i in range(1, 5)
    ]
    gas = [
        prop(
            db,
            "Project Matador Gas Plant (PMG)",
            mw=mw,
            technology="gas_ct",
            sponsor=fermi_a,
            location=where,
            record=f"69799-EG{i:03d}",
        )
        for i, mw in enumerate((260, 40, 40), start=1)
    ]
    report = build.rebuild_sites(db, now=T0)
    db.commit()
    assert (report.sites, report.members, report.created) == (1, 7, 1)
    (site,) = live_sites(db)
    rows = members_of(db, site)
    lead = min(nuclear, key=lambda p: p.public_id)
    assert site.lead_proposal_id == lead.id
    assert rows[lead.public_id].relation == "lead" and rows[lead.public_id].group_key == "eia:69798"
    for p in nuclear[1:] if nuclear[0] is lead else [p for p in nuclear if p is not lead]:
        assert (rows[p.public_id].relation, rows[p.public_id].parent_proposal_id) == ("unit_of", lead.id)
    gas_head = gas[0]
    assert rows[gas_head.public_id].relation == "co_located"
    assert rows[gas_head.public_id].parent_proposal_id is None
    for p in gas[1:]:
        assert (rows[p.public_id].relation, rows[p.public_id].parent_proposal_id) == ("unit_of", gas_head.id)
    assert site.anchors["eia_plant_ids"] == ["69798", "69799"]
    assert {r.grouping_rule for r in rows.values()} == {"eia_plant"}


def test_poi_phases_group_and_competitors_do_not(db: Session) -> None:
    iv = point(db, "Manning-Midway 500kV Line")
    a = prop(db, "Darden I Solar", mw=300, poi=iv, queue_date="2021-04-15")
    b = prop(db, "Darden II Solar", mw=300, poi=iv, queue_date="2021-04-16")
    rival = prop(db, "Westlands Solar", mw=900, poi=iv, sponsor=org(db, "Other Developer"))
    alone = prop(db, "Nowhere Solar")
    build.rebuild_sites(db, now=T0)
    db.commit()
    (site,) = live_sites(db)
    rows = members_of(db, site)
    assert set(rows) == {a.public_id, b.public_id}
    assert rows[b.public_id].relation == "lead"  # equal MW: the later filing leads
    assert rows[a.public_id].relation == "phase_of"
    assert rows[a.public_id].grouping_rule == "poi_stem"
    stored = {r.proposal_id for r in db.scalars(select(SiteMember))}
    assert rival.id not in stored and alone.id not in stored


def test_st_gall_iiia_and_iiib_are_phases_not_an_unclear_pair(db: Session) -> None:
    """The beta store labelled these `same_site` (low): "IIIA" was not read as a phase marker and
    kept the two names' stems apart. Both now read as numbered phases of `gall` (lane S2)."""
    iv = point(db, "St Gall 115kV")
    dev = org(db, "Gall Developer LLC")
    a = prop(db, "st gall IIIA Storage", mw=200, poi=iv, sponsor=dev, technology="storage")
    b = prop(db, "st gall IIIB Storage", mw=100, poi=iv, sponsor=dev, technology="storage")
    build.rebuild_sites(db, now=T0)
    db.commit()
    (site,) = live_sites(db)
    rows = members_of(db, site)
    assert rows[a.public_id].relation == "lead"
    assert (rows[b.public_id].relation, rows[b.public_id].confidence) == ("phase_of", "high")
    assert rows[b.public_id].basis["phases"] == ["3B"]
    assert site.rule_version == rules.RULE_VERSION


def test_each_membership_stores_its_direct_links_but_not_a_shared_plant(db: Session) -> None:
    """The per-edge evidence the API needs to recompute a viewer's connectivity: each member's rule
    b-c partners by public id. Units of one plant need none (the plant id joins them)."""
    iv = point(db)
    dev = org(db, "Linked Dev")
    a = prop(db, "Linked Solar", poi=iv, sponsor=dev)
    b = prop(db, "Linked Storage", poi=iv, sponsor=dev, technology="storage")
    u1 = prop(db, "Unit Plant", record="4242-1", technology="gas_ct")
    u2 = prop(db, "Unit Plant", record="4242-2", technology="gas_ct")
    build.rebuild_sites(db, now=T0)
    db.commit()
    rows = {
        db.get(Proposal, r.proposal_id).public_id: r  # type: ignore[union-attr]
        for r in db.scalars(select(SiteMember))
    }
    assert len(live_sites(db)) == 2
    assert rows[a.public_id].grouping_evidence[rules.LINKS_KEY] == {b.public_id: "poi_sponsor"}
    assert rows[b.public_id].grouping_evidence[rules.LINKS_KEY] == {a.public_id: "poi_sponsor"}
    assert rows[u1.public_id].grouping_evidence[rules.LINKS_KEY] == {}
    assert rows[u2.public_id].grouping_evidence[rules.LINKS_KEY] == {}


def test_a_rerun_writes_nothing_and_keeps_ids(db: Session) -> None:
    iv = point(db)
    s1 = org(db, "Hiru Dev")
    prop(db, "Hiru Solar", poi=iv, sponsor=s1)
    prop(db, "Hiru Storage", poi=iv, sponsor=s1, technology="storage")
    build.rebuild_sites(db, now=T0)
    db.commit()
    before = [(s.public_id, s.updated_at) for s in live_sites(db)]
    again = build.rebuild_sites(db, now=T1)
    db.commit()
    assert (again.created, again.updated, again.unchanged, again.members_written, again.retired) == (
        0,
        0,
        1,
        0,
        0,
    )
    assert [(s.public_id, s.updated_at) for s in live_sites(db)] == before
    assert db.scalar(select(SiteAudit.kind).where(SiteAudit.recorded_at == T1)) is None


def test_a_larger_filing_takes_the_lead_and_the_site_keeps_its_id(db: Session) -> None:
    iv = point(db)
    dev = org(db, "Roma Dev")
    first = prop(db, "Roma Storage", mw=255, poi=iv, sponsor=dev, technology="storage")
    prop(db, "Roma Storage 2", mw=100, poi=iv, sponsor=dev, technology="storage")
    build.rebuild_sites(db, now=T0)
    db.commit()
    (site,) = live_sites(db)
    big = prop(db, "Roma Storage 3", mw=900, poi=iv, sponsor=dev, technology="storage")
    build.rebuild_sites(db, now=T1)
    db.commit()
    (again,) = live_sites(db)
    assert again.public_id == site.public_id
    assert again.lead_proposal_id == big.id
    kinds = {a.kind: a.detail for a in db.scalars(select(SiteAudit).where(SiteAudit.recorded_at == T1))}
    assert kinds["members_gained"] == {"members": [big.public_id]}
    assert kinds["lead_changed"] == {"from": first.public_id, "to": big.public_id}


def test_a_split_keeps_the_id_on_the_larger_part_and_is_audited(db: Session) -> None:
    iv = point(db)
    dev = org(db, "Tempus Power")
    keep = [prop(db, f"Pioneer Path Storage {i}", poi=iv, sponsor=dev) for i in range(1, 4)]
    leave = [prop(db, f"Prairie Horizon Storage {i}", poi=iv, sponsor=dev) for i in range(1, 3)]
    build.rebuild_sites(db, now=T0)
    db.commit()
    (site,) = live_sites(db)
    other = org(db, "Someone Else Entirely")
    for p in leave:
        p.sponsor_org_id = other.id
    db.flush()
    report = build.rebuild_sites(db, now=T1)
    db.commit()
    assert (report.created, report.retired) == (1, 0)
    sites = live_sites(db)
    assert len(sites) == 2
    kept = next(s for s in sites if s.public_id == site.public_id)
    assert set(members_of(db, kept)) == {p.public_id for p in keep}
    new = next(s for s in sites if s.public_id != site.public_id)
    assert set(members_of(db, new)) == {p.public_id for p in leave}
    split = db.scalar(select(SiteAudit).where(SiteAudit.kind == "split"))
    assert split is not None and split.site_id == site.id
    assert split.detail["into"] == {
        site.public_id: sorted(p.public_id for p in keep),
        new.public_id: sorted(p.public_id for p in leave),
    }


def test_a_merge_keeps_the_older_id_and_retires_the_other_with_a_successor(db: Session) -> None:
    a_pt, b_pt = point(db, "Alpha 230kV"), point(db, "Bravo 230kV")
    dev = org(db, "Merge Dev")
    a = [prop(db, f"Kestrel Solar {i}", poi=a_pt, sponsor=dev) for i in (1, 2)]
    prop(db, "Kestrel Storage", poi=b_pt, sponsor=dev, technology="storage")
    b2 = prop(db, "Kestrel Storage 2", poi=b_pt, sponsor=dev, technology="storage")
    build.rebuild_sites(db, now=T0)
    db.commit()
    older, younger = live_sites(db)  # both created at T0: ordered by public id
    b2.interconnection_point_id = a_pt.id
    db.flush()
    report = build.rebuild_sites(db, now=T1)
    db.commit()
    assert report.sites == 1
    (survivor,) = live_sites(db)
    retired = db.scalars(select(Site).where(Site.retired_at.is_not(None))).all()
    assert len(retired) == 1
    assert retired[0].successor_site_id == survivor.id
    assert retired[0].member_count == 0 and retired[0].lead_proposal_id is None
    assert {survivor.public_id, retired[0].public_id} == {older.public_id, younger.public_id}
    kinds = {a.kind for a in db.scalars(select(SiteAudit).where(SiteAudit.recorded_at == T1))}
    assert {"merged", "retired", "members_gained"} <= kinds
    assert a  # the first pair stays in the survivor
    assert {p.public_id for p in a} <= set(members_of(db, survivor))


def test_a_site_that_loses_its_grouping_is_retired_and_its_id_never_reused(db: Session) -> None:
    iv = point(db)
    dev = org(db, "Short Lived Dev")
    x = prop(db, "Lonely Solar", poi=iv, sponsor=dev)
    y = prop(db, "Lonely Storage", poi=iv, sponsor=dev, technology="storage")
    build.rebuild_sites(db, now=T0)
    db.commit()
    (site,) = live_sites(db)
    y.merged_into_id = x.id  # the resolver merged them: one live record, no site of one
    db.flush()
    report = build.rebuild_sites(db, now=T1)
    db.commit()
    assert (report.sites, report.retired, report.members_removed) == (0, 1, 2)
    assert db.get(Site, site.id).retired_at is not None  # type: ignore[union-attr]
    retired = db.scalar(select(SiteAudit).where(SiteAudit.kind == "retired"))
    assert retired is not None and retired.detail == {
        "members": sorted([x.public_id, y.public_id]),
        "successor": None,
    }
    y.merged_into_id = None
    db.flush()
    build.rebuild_sites(db, now=T2)
    db.commit()
    (reborn,) = live_sites(db)
    assert reborn.public_id != site.public_id


def test_anchors_name_the_power_plant_asset_of_an_eia_plant(db: Session) -> None:
    db.add(
        Asset(
            public_id="asset_0000000001",
            slug="riverton",
            asset_type="power_plant",
            source_asset_id="1239",
            name="Riverton",
            country="US",
            source_id="us.eia.860m",
            source_url="https://example.org/eia",
            retrieved_at=T0,
            licence_id="open",
        )
    )
    db.flush()
    asset = db.scalar(select(Asset))
    prop(db, "Riverton", record="1239-7", technology="gas_ct")
    prop(db, "Riverton", record="1239-8", technology="gas_ct")
    build.rebuild_sites(db, now=T0)
    db.commit()
    (site,) = live_sites(db)
    assert site.anchors == {
        "eia_plant_ids": ["1239"],
        "interconnection_point_ids": [],
        "assets": {"1239": str(asset.id)},  # type: ignore[union-attr]
    }


def test_an_oversize_chain_is_flagged(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rules, "OVERSIZE_MEMBERS", 3)
    iv = point(db)
    dev = org(db, "Chain Dev")
    for i in range(5):
        prop(db, f"Chain Project Number {i}", poi=iv, sponsor=dev)
    report = build.rebuild_sites(db, now=T0)
    db.commit()
    (site,) = live_sites(db)
    assert site.review_flag == "oversize"
    assert report.flagged == [site.public_id]


def test_run_reports_counts_for_the_resolve_tick(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        _registry(s)
        dev = org(s, "Tick Dev")
        iv = point(s)
        prop(s, "Tick Solar", poi=iv, sponsor=dev)
        prop(s, "Tick Storage", poi=iv, sponsor=dev, technology="storage")
        s.commit()
    out: dict[str, Any] = build.run(factory)
    assert out["sites"] == 1 and out["created"] == 1 and out["flagged"] == 0
    assert build.run(factory)["unchanged"] == 1


def test_the_cli_dry_run_writes_nothing(
    factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    from services.sites import __main__ as cli

    with factory() as s:
        _registry(s)
        dev = org(s, "Cli Dev")
        iv = point(s)
        prop(s, "Cli Solar", poi=iv, sponsor=dev)
        prop(s, "Cli Storage", poi=iv, sponsor=dev, technology="storage")
        s.commit()
        candidates = build.make_candidates(*build.read_inputs(s))
    text = cli.render(build.plan(candidates), top=1, sample=1, show=["Cli Solar"])
    assert '"sites": 1' in text and "Sites holding 'Cli Solar': 1" in text
    with factory() as s:
        assert s.scalar(select(Site)) is None
    del capsys


def test_the_cli_rebuilds_or_measures_a_store_at_a_url(
    tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    from services.db.session import get_engine, init_db
    from services.sites import __main__ as cli

    url = f"sqlite+pysqlite:///{tmp_path / 'sites.db'}"
    engine = get_engine(url)
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        _registry(s)
        dev = org(s, "Url Dev")
        iv = point(s)
        prop(s, "Url Solar", poi=iv, sponsor=dev)
        prop(s, "Url Storage", poi=iv, sponsor=dev, technology="storage")
        s.commit()
    assert cli.main(["--dry-run", "--database-url", url, "--sample", "0"]) == 0
    with get_sessionmaker(engine)() as s:
        assert s.scalar(select(Site)) is None
    assert cli.main(["--database-url", url, "--top", "1", "--sample", "1", "--show", "Url Solar"]) == 0
    out = capsys.readouterr().out
    assert 'written: {"sites": 1' in out and "Largest 1 sites" in out and "random sites (seed 7)" in out
    with get_sessionmaker(engine)() as s:
        assert s.scalar(select(Site)) is not None
    engine.dispose()
