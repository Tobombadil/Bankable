"""`GET /v1/sites/{public_id}` and the proposal detail's `site` embed (`services/api/sites.py`):
contract, visibility (a PJM member hidden from the public; a site with one visible member shows
nothing), the viewer's own lead, neighbours, anchors, sponsors, retirement, review and the kill
switch."""

from __future__ import annotations

import datetime as dt
import pathlib
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from sqlalchemy.orm import Session

from services.api.conftest import (
    make_asset,
    make_asset_owner,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_proposal,
)
from services.db.models import InterconnectionPoint, Licence, Proposal, ProposalSource, Site, Source
from services.sites import build
from services.sites.switch import ENV_VAR

UTC = dt.UTC
SPEC = pathlib.Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return yaml.safe_load(SPEC.read_text(encoding="utf-8"))


def assert_valid(spec: dict[str, Any], name: str, instance: object) -> None:
    """Against `components.schemas[name]` of the committed spec (`api/check_story_coverage.py`'s way)."""
    base = "https://spec.local/openapi.yaml"
    registry = Registry().with_resource(uri=base, resource=Resource(contents=spec, specification=DRAFT202012))
    validator = Draft202012Validator({"$ref": f"{base}#/components/schemas/{name}"}, registry=registry)
    errors = sorted(validator.iter_errors(instance), key=str)
    assert not errors, "\n".join(f"{e.message} at {list(e.absolute_path)}" for e in errors)


def _point(db: Session, source: Source, name: str = "Midway 500kV") -> InterconnectionPoint:
    pt = InterconnectionPoint(
        public_id=f"poi_{abs(hash(name)) % 10**10:010d}",
        operator="CAISO",
        name_display=name,
        name_key=name.lower(),
        key_rule="t",
        kind="substation",
        source_id=source.id,
        source_url=source.url,
        retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        licence_id=source.licence_id,
    )
    db.add(pt)
    db.flush()
    return pt


def _member(
    db: Session,
    source: Source,
    suffix: str,
    name: str,
    *,
    mw: float,
    sponsor: Any,
    point: Any,
    technology: str = "solar",
) -> Proposal:
    p = make_visible_proposal(db, source, public_id_suffix=suffix, sponsor=sponsor, technology=technology)
    p.name_canonical = name
    p.capacity_mw = mw
    p.interconnection_point_id = point.id
    db.flush()
    return p


@pytest.fixture()
def seeded(db: Session) -> dict[str, Any]:
    """Three records of one developer at one point (a site), a rival developer's request at the same
    point (a neighbour, not a member), and a singleton elsewhere."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    dev = make_org(db, "Kestrel Renewables LLC")
    rival = make_org(db, "Other Developer Inc")
    pt = _point(db, src)
    lead = _member(db, src, "1", "Kestrel Solar 2", mw=500, sponsor=dev, point=pt)
    phase = _member(db, src, "2", "Kestrel Solar 1", mw=300, sponsor=dev, point=pt)
    storage = _member(db, src, "3", "Kestrel Storage", mw=200, sponsor=dev, point=pt, technology="storage")
    neighbour = _member(db, src, "4", "Westlands Solar", mw=900, sponsor=rival, point=pt)
    single = make_visible_proposal(db, src, public_id_suffix="5")
    build.rebuild_sites(db)
    db.commit()
    site = db.query(Site).one()
    return {
        "lic": lic, "src": src, "dev": dev, "pt": pt, "site": site,
        "lead": lead, "phase": phase, "storage": storage, "neighbour": neighbour, "single": single,
    }  # fmt: skip


def test_site_detail_matches_the_contract_and_lists_members_lead_first(
    client: Any, seeded: dict[str, Any], spec: dict[str, Any]
) -> None:
    resp = client.get(f"/v1/sites/{seeded['site'].public_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert_valid(spec, "SiteDetailResponse", body)
    data = body["data"]
    assert data["name"] == "Kestrel Solar 2"
    assert data["members"][0]["public_id"] == seeded["lead"].public_id
    relations = {m["public_id"]: m["relation"] for m in data["members"]}
    assert relations == {
        seeded["lead"].public_id: "lead",
        seeded["phase"].public_id: "phase_of",
        seeded["storage"].public_id: "co_located",
    }
    assert data["totals"]["member_count"] == 3 and data["totals"]["active_mw"] == 1000.0
    assert [n["public_id"] for n in data["shares_interconnection_point"]] == [seeded["neighbour"].public_id]
    assert data["shares_interconnection_point"][0]["relation"] == "shares_interconnection_point"
    assert data["anchors"]["interconnection_points"][0]["public_id"] == seeded["pt"].public_id
    assert [(o["name_canonical"], o["member_count"]) for o in data["sponsors"]] == [
        ("Kestrel Renewables LLC", 3)
    ]
    credited = {s["source_id"] for s in body["licence_summary"]["sources"]}
    assert seeded["src"].id in credited


def test_proposal_detail_carries_the_site_embed(
    client: Any, seeded: dict[str, Any], spec: dict[str, Any]
) -> None:
    body = client.get(f"/v1/proposals/{seeded['phase'].public_id}").json()
    assert_valid(spec, "ProposalDetailResponse", body)
    site = body["data"]["site"]
    assert site["public_id"] == seeded["site"].public_id
    assert (site["relation"], site["is_lead"], site["member_count"]) == ("phase_of", False, 3)
    assert site["lead"]["public_id"] == seeded["lead"].public_id
    assert site["grouping_rule"] == "poi_sponsor"
    assert client.get(f"/v1/proposals/{seeded['single'].public_id}").json()["data"]["site"] is None
    assert client.get(f"/v1/proposals/{seeded['neighbour'].public_id}").json()["data"]["site"] is None


def _pjm(db: Session) -> Source:
    lic = Licence(
        id="pjm-lic",
        name="PJM terms",
        reuse_class="restricted",
        allows_derived_publication=False,
        allows_raw_publication=False,
        allows_api_redistribution=False,
        allows_bulk_export=False,
        allows_commercial_use=False,
        gate_flag=True,
        evidence_url="https://example.org/pjm",
        evidence_retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        classified_by="legal-compliance",
    )
    db.add(lic)
    db.flush()
    return make_public_source(db, lic, "us.iso.pjm.gen_queue")


def test_a_pjm_member_is_hidden_from_a_public_caller_and_the_lead_is_theirs(
    client: Any, db: Session, seeded: dict[str, Any]
) -> None:
    """A PJM row (not public until a licence exists) that would lead the site: the public caller
    sees the site without it, headed by the largest member they may see."""
    pjm = _pjm(db)
    big = make_visible_proposal(db, pjm, public_id_suffix="9", sponsor=seeded["dev"], technology="solar")
    big.name_canonical, big.capacity_mw, big.min_reuse_class = "Kestrel PJM Mega", 5000, "restricted"
    big.interconnection_point_id = seeded["pt"].id
    db.flush()
    build.rebuild_sites(db)
    db.commit()
    site = db.get(Site, seeded["site"].id)
    assert site is not None and site.lead_proposal_id == big.id  # the stored lead is the PJM row
    data = client.get(f"/v1/sites/{site.public_id}").json()["data"]
    assert data["name"] == "Kestrel Solar 2"
    assert big.public_id not in {m["public_id"] for m in data["members"]}
    assert data["member_count"] == 3 and data["members"][0]["relation"] == "lead"
    assert data["totals"]["active_mw"] == 1000.0
    assert "Kestrel PJM Mega" not in str(data)
    embed = client.get(f"/v1/proposals/{seeded['lead'].public_id}").json()["data"]["site"]
    assert embed["is_lead"] is True and embed["member_count"] == 3


def _bridged(db: Session) -> dict[str, Any]:
    """Two pairs of one developer's records joined only by a PJM row (restricted: no public tier sees
    it). A1/A2 and the PJM row share a grid connection point; B1/B2 and the PJM row share an exact
    location. A and B share nothing directly, so without the PJM row there are two groups."""
    from services.api.conftest import make_location

    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    pjm = _pjm(db)
    dev = make_org(db, "Bridge Renewables LLC")
    pt = _point(db, src, "Bridge 345kV")

    def at(where: Any) -> Any:
        return make_location(db, src, lic, geom=where, precision="exact", county_name=None)

    a1 = _member(db, src, "1", "Alder Solar", mw=500, sponsor=dev, point=pt)
    a2 = _member(db, src, "2", "Alder Storage", mw=100, sponsor=dev, point=pt, technology="storage")
    b1 = make_visible_proposal(
        db, src, public_id_suffix="3", sponsor=dev, technology="solar", location=at((-75.0, 40.0))
    )
    b2 = make_visible_proposal(
        db, src, public_id_suffix="4", sponsor=dev, technology="storage", location=at((-75.0, 40.0))
    )
    b1.name_canonical, b1.capacity_mw = "Birch Solar", 300
    b2.name_canonical, b2.capacity_mw = "Birch Storage", 50
    bridge = make_visible_proposal(
        db, pjm, public_id_suffix="9", sponsor=dev, technology="solar", location=at((-75.0, 40.0))
    )
    bridge.name_canonical, bridge.capacity_mw, bridge.min_reuse_class = (
        "Gated Bridge Mega",
        5000,
        "restricted",
    )
    bridge.interconnection_point_id = pt.id
    db.flush()
    build.rebuild_sites(db)
    db.commit()
    site = db.query(Site).one()
    return {"site": site, "a1": a1, "a2": a2, "b1": b1, "b2": b2, "bridge": bridge}


def test_no_bridging_through_a_gated_record(client: Any, db: Session, spec: dict[str, Any]) -> None:
    """The gap lane S left: a visible record grouped only through a hidden one appeared in the site.
    Now each public caller sees only a group its own visible evidence links, with its own lead."""
    w = _bridged(db)
    site = w["site"]
    assert site.member_count == 5 and site.lead_proposal_id == w["bridge"].id
    body = client.get(f"/v1/sites/{site.public_id}").json()
    assert_valid(spec, "SiteDetailResponse", body)
    data = body["data"]
    # The largest visible group (a tie on size: the stronger lead), and a note that it is partial.
    assert [m["public_id"] for m in data["members"]] == [w["a1"].public_id, w["a2"].public_id]
    assert (data["name"], data["member_count"], data["partial"]) == ("Alder Solar", 2, True)
    assert data["totals"]["active_mw"] == 600.0
    assert "Birch" not in str(data) and "Gated Bridge" not in str(data)
    # The other group, by naming one of its members (the proposal page's panel asks this way).
    other = client.get(f"/v1/sites/{site.public_id}", params={"member": w["b2"].public_id}).json()["data"]
    assert [m["public_id"] for m in other["members"]] == [w["b1"].public_id, w["b2"].public_id]
    assert other["members"][1]["relation"] == "co_located" and other["partial"] is True
    # A hidden member, or a record that is not a member, is the unknown id's 404.
    for named in (w["bridge"].public_id, "prop_0000000000"):
        resp = client.get(f"/v1/sites/{site.public_id}", params={"member": named})
        assert resp.status_code == 404
    # Each record's embed counts and leads its own group.
    embed_b = client.get(f"/v1/proposals/{w['b2'].public_id}").json()["data"]["site"]
    assert (embed_b["member_count"], embed_b["lead"]["public_id"]) == (2, w["b1"].public_id)
    embed_a = client.get(f"/v1/proposals/{w['a2'].public_id}").json()["data"]["site"]
    assert (embed_a["member_count"], embed_a["lead"]["public_id"]) == (2, w["a1"].public_id)


def test_a_group_of_one_after_the_bridge_is_hidden_has_no_site(client: Any, db: Session) -> None:
    w = _bridged(db)
    w["b2"].publish_state = "unpublished"
    db.commit()
    assert client.get(f"/v1/proposals/{w['b1'].public_id}").json()["data"]["site"] is None
    data = client.get(f"/v1/sites/{w['site'].public_id}").json()["data"]
    assert [m["public_id"] for m in data["members"]] == [w["a1"].public_id, w["a2"].public_id]
    assert data["partial"] is True  # B1 is visible, and is not listed


def test_rows_written_before_links_were_stored_fail_closed(client: Any, db: Session) -> None:
    """A membership row with no stored links joins others by plant id only while a member is hidden:
    the connectivity it would need cannot be proven, so it is not assumed (one rebuild fixes it)."""
    from services.db.models import SiteMember

    w = _bridged(db)
    for row in db.query(SiteMember):
        row.grouping_evidence = {k: v for k, v in row.grouping_evidence.items() if k != "links"}
    db.commit()
    assert client.get(f"/v1/sites/{w['site'].public_id}").status_code == 404
    assert client.get(f"/v1/proposals/{w['a1'].public_id}").json()["data"]["site"] is None


def test_with_every_member_visible_the_stored_site_is_served_whole(client: Any, db: Session) -> None:
    w = _bridged(db)
    w["bridge"].min_reuse_class = "open"
    for link in w["bridge"].sources:
        link.source.licence.reuse_class = "open"
    db.commit()
    data = client.get(f"/v1/sites/{w['site'].public_id}").json()["data"]
    assert data["member_count"] == 5 and data["partial"] is False
    assert data["members"][0]["public_id"] == w["bridge"].public_id


def test_a_site_with_one_visible_member_shows_nothing(
    client: Any, db: Session, seeded: dict[str, Any]
) -> None:
    for p in (seeded["phase"], seeded["storage"]):
        p.publish_state = "unpublished"
    db.commit()
    assert client.get(f"/v1/sites/{seeded['site'].public_id}").status_code == 404
    assert client.get(f"/v1/proposals/{seeded['lead'].public_id}").json()["data"]["site"] is None


def test_unknown_flagged_and_retired_sites(client: Any, db: Session, seeded: dict[str, Any]) -> None:
    assert client.get("/v1/sites/site_0000000000").status_code == 404
    site = db.get(Site, seeded["site"].id)
    assert site is not None
    site.review_flag = "oversize"
    db.commit()
    assert client.get(f"/v1/sites/{site.public_id}").status_code == 404
    site.review_flag = None
    # Retire it with a visible successor: the old id answers 301 there.
    successor = Site(
        public_id="site_0000000099", slug="succ", name_display="x", rule_version="t",
        member_count=site.member_count, lead_proposal_id=site.lead_proposal_id,
    )  # fmt: skip
    db.add(successor)
    db.flush()
    from services.db.models import SiteMember

    for row in db.query(SiteMember).filter(SiteMember.site_id == site.id):
        row.site_id = successor.id
    site.retired_at = dt.datetime.now(UTC)
    site.successor_site_id = successor.id
    db.commit()
    resp = client.get(f"/v1/sites/{site.public_id}", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"].endswith("/v1/sites/site_0000000099")


def test_the_kill_switch_hides_every_site(
    client: Any, seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_VAR, "0")
    assert client.get(f"/v1/sites/{seeded['site'].public_id}").status_code == 404
    assert client.get(f"/v1/proposals/{seeded['lead'].public_id}").json()["data"]["site"] is None
    monkeypatch.setenv(ENV_VAR, "1")
    assert client.get(f"/v1/sites/{seeded['site'].public_id}").status_code == 200
    monkeypatch.delenv(ENV_VAR)
    assert client.get(f"/v1/proposals/{seeded['lead'].public_id}").json()["data"]["site"] is not None


def test_the_admin_review_lists_recent_audit_rows_and_flagged_sites(
    client: Any, db: Session, seeded: dict[str, Any], spec: dict[str, Any]
) -> None:
    from tests.conftest import login, make_account, make_user

    assert client.get("/admin/v1/sites/review").status_code == 401
    site = seeded["site"]
    site.review_flag = "oversize"
    operator = make_user(
        db, make_account(db, entitlement="admin", name="Ops"), email="ops@example.com", role="operator"
    )
    db.commit()
    login(client, db, operator)
    body = client.get("/admin/v1/sites/review", params={"limit": 5}).json()
    assert_valid(spec, "AdminSiteReviewResponse", body)
    (created,) = body["data"]["audit"]
    assert created["kind"] == "created" and created["site"]["public_id"] == site.public_id
    assert set(created["detail"]["members"]) == {seeded[k].public_id for k in ("lead", "phase", "storage")}
    assert [s["public_id"] for s in body["data"]["flagged"]] == [site.public_id]
    assert body["data"]["flagged"][0]["review_flag"] == "oversize"
    assert client.get("/admin/v1/sites/review", params={"bogus": "1"}).status_code == 400


def test_anchors_link_the_eia_plant_asset_and_its_owner(
    client: Any, db: Session, spec: dict[str, Any]
) -> None:
    lic = make_open_licence(db)
    eia = make_public_source(db, lic, "us.eia.860m")
    owner = make_org(db, "Liberty Utilities Co")
    asset = make_asset(db, eia, lic, source_asset_id="1239", name="Riverton", technology="gas_cc")
    make_asset_owner(db, asset, owner, eia, lic)
    units = []
    for suffix, gen in (("1", "7"), ("2", "8")):
        p = make_visible_proposal(db, eia, public_id_suffix=suffix, technology="gas_ct")
        p.name_canonical = "Riverton"
        link = db.query(ProposalSource).filter(ProposalSource.proposal_id == p.id).one()
        link.source_record_id = f"1239-{gen}"
        units.append(p)
    db.flush()
    build.rebuild_sites(db)
    db.commit()
    site = db.query(Site).one()
    body = client.get(f"/v1/sites/{site.public_id}").json()
    assert_valid(spec, "SiteDetailResponse", body)
    anchors = body["data"]["anchors"]
    assert anchors["eia_plant_ids"] == ["1239"]
    assert anchors["assets"][0]["public_id"] == asset.public_id
    assert anchors["assets"][0]["owners"][0]["organization"]["name_canonical"] == "Liberty Utilities Co"
    members = body["data"]["members"]
    assert members[1]["relation"] == "unit_of"
    assert members[1]["parent_public_id"] == members[0]["public_id"]
    assert members[0]["group"] == {"key": "eia:1239", "eia_plant_ids": ["1239"]}
