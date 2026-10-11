"""The M-11 audit over sites (`services/visibility_audit/run.py`, "Sites"; lane S2, 2026-10-10).

A site is one more way a gated row could be reached: its members, its lead, the relation a hidden
member carries, its neighbours and anchors, the `site` embed on every proposal, and the site's web
pages. Each test seeds two pairs of one developer's records joined only by a PJM row (restricted, and
`publication: none` in the real `data/sources.yaml`), proves the clean store reads `m11 = 0` with the
site probed, then stands in one regression at a time and proves the audit finds it.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import services.api.sites as sites_api
from services.api.app import app
from services.api.conftest import (
    make_location,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_proposal,
)
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.db.models import InterconnectionPoint, Licence, Proposal, Site, Source
from services.db.session import get_sessionmaker
from services.sites import build, rules
from services.visibility_audit import run
from tests.conftest import login, make_account, make_user
from tests.db_template import disposing, fresh_engine

UTC = dt.UTC
SPEC = pathlib.Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"
PJM = "us.iso.pjm.gen_queue"


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLATFORM_POSTURE", raising=False)
    monkeypatch.delenv("SITES_ENABLED", raising=False)
    default_limiter.reset()


@pytest.fixture()
def factory() -> Iterator[sessionmaker[Session]]:
    with disposing(fresh_engine()) as engine:
        yield get_sessionmaker(engine)


@pytest.fixture()
def db(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as s:
        yield s


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
    src = make_public_source(db, lic, PJM)
    src.publish_state = "ingest_only"  # as the loader leaves it
    db.flush()
    return src


def seed(db: Session) -> dict[str, Any]:
    """A1/A2 and the PJM row share a grid connection point; B1/B2 and the PJM row share an exact
    location; one developer throughout. Over public records, A and B are two groups."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    pjm = _pjm(db)
    dev = make_org(db, "Bridge Renewables LLC")
    pt = InterconnectionPoint(
        public_id="poi_0000000042",
        operator="CAISO",
        name_display="Bridge 345kV",
        name_key="bridge 345kv",
        key_rule="t",
        kind="substation",
        source_id=src.id,
        source_url=src.url,
        retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        licence_id=lic.id,
    )
    db.add(pt)
    db.flush()

    def record(source: Source, suffix: str, name: str, mw: float, technology: str, **kw: Any) -> Proposal:
        where = kw.pop("where", None)
        location = (
            make_location(db, source, source.licence, geom=where, precision="exact", county_name=None)
            if where
            else None
        )
        p = make_visible_proposal(
            db, source, public_id_suffix=suffix, sponsor=dev, technology=technology, location=location
        )
        p.name_canonical, p.capacity_mw = name, mw
        if kw.get("point"):
            p.interconnection_point_id = pt.id
        db.flush()
        return p

    out = {
        "a1": record(src, "1", "Alder Solar", 500, "solar", point=True),
        "a2": record(src, "2", "Alder Storage", 100, "storage", point=True),
        "b1": record(src, "3", "Birch Solar", 300, "solar", where=(-75.0, 40.0)),
        "b2": record(src, "4", "Birch Storage", 50, "storage", where=(-75.0, 40.0)),
        "bridge": record(pjm, "9", "Gated Bridge Mega", 5000, "solar", point=True, where=(-75.0, 40.0)),
    }
    out["bridge"].min_reuse_class = "restricted"
    db.flush()
    build.rebuild_sites(db)
    db.commit()
    out["site"] = db.query(Site).one()
    return out


def _breaches(result: dict[str, Any], prefix: str = "") -> list[dict[str, Any]]:
    return [b for b in result["breaches"] if b["surface"] == run.SITES and b["reason"].startswith(prefix)]


# ================================================================================ clean store
def test_a_site_with_a_gated_bridge_is_probed_and_reads_m11_zero(
    db: Session, factory: sessionmaker[Session]
) -> None:
    w = seed(db)
    assert w["site"].member_count == 5  # the stored site holds the PJM row

    result = run.run_audit(factory)

    assert result["m11"] == 0, result["breaches"]
    assert result["counts"][run.SITES] == {"shown": 1, "breaches": 0}
    site_checks = [c for c in result["served"]["checks"] if c["surface"] == run.SITES]
    paths = {c["path"] for c in site_checks}
    site_id = w["site"].public_id
    assert {f"/v1/sites/{site_id}", f"/v1/proposals/{w['a1'].public_id}", f"/sites/{site_id}"} <= paths
    assert f"/proposals/{w['a1'].slug}" in paths  # the record page that carries the panel
    assert all(c["status"] == 200 and not c["leak"] for c in site_checks), site_checks
    assert result["served"]["web_pages"] == "checked"


def test_the_persisted_result_matches_the_contract(db: Session, factory: sessionmaker[Session]) -> None:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    seed(db)
    operator = make_user(
        db, make_account(db, entitlement="admin", name="Ops"), email="o@example.com", role="operator"
    )
    db.commit()
    run.run_audit(factory)

    def _override() -> Iterator[Session]:
        with factory() as s:
            yield s

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            login(client, db, operator)
            body = client.get("/admin/v1/visibility-audits/latest").json()
    finally:
        app.dependency_overrides.pop(get_db, None)
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    base = "https://spec.local/openapi.yaml"
    registry = Registry().with_resource(uri=base, resource=Resource(contents=spec, specification=DRAFT202012))
    validator = Draft202012Validator(
        {"$ref": f"{base}#/components/schemas/VisibilityAuditResponse"}, registry=registry
    )
    assert not list(validator.iter_errors(body))
    assert body["data"]["counts"]["sites"]["shown"] == 1


# ============================================================================ stood-in regressions
def test_a_bridge_through_the_hidden_record_is_found_and_shown_to_be_served(
    db: Session, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap lane S2 closed, reopened: the API joins every visible member in one group again."""
    w = seed(db)
    monkeypatch.setattr(rules, "connected_groups", lambda members, links: [list(members)])

    result = run.run_audit(factory, persist=False)

    bridge = _breaches(result, "site_bridge_printed:")
    assert [b["public_id"] for b in bridge] == [w["site"].public_id]
    embeds = {b["public_id"] for b in _breaches(result, "site_embed_printed:proposal_embed:member_count")}
    assert embeds == {w[k].public_id for k in ("a1", "a2", "b1", "b2")}
    assert _breaches(result, "site_embed_printed:bulk_embed:")
    assert bridge[0]["served_status"] == 200 and bridge[0]["served_leak"] is True
    assert result["m11"] == result["breach_total"] > 0


def test_a_gated_member_reachable_through_a_site_is_found(
    db: Session, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A predicate regression in the site route only: the restricted PJM row is served as a member
    and as the lead, through the site and through every member's embed."""
    w = seed(db)
    monkeypatch.setattr(
        sites_api,
        "proposal_visibility_filter",
        lambda entitlement="public", now=None: [Proposal.publish_state == "public"],
    )

    result = run.run_audit(factory, persist=False)

    page = _breaches(result, "site_member_printed:")
    assert [b["public_id"] for b in page] == [w["site"].public_id]
    assert page[0]["served_leak"] is True
    lead = {b["public_id"] for b in _breaches(result, "site_embed_printed:proposal_embed:lead_not_visible")}
    assert lead == {w[k].public_id for k in ("a1", "a2", "b1", "b2")}


def test_a_serving_path_that_bypasses_the_builders_is_caught_by_the_served_pass(
    db: Session, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store pass renders through `served_groups`; the route alone is regressed to serve every
    stored member. Only the served pass can see it: on the API detail and on the site's web page."""
    w = seed(db)
    real = sites_api.served_groups

    def everyone(
        db: Session, site: Site, entitlement: str, *, with_sources: bool = True, member: Any = None
    ) -> Any:
        groups = real(db, site, "admin-bypass", with_sources=with_sources)  # an unknown tier: no filter
        return groups[0] if groups else None

    monkeypatch.setattr(sites_api, "served_site", everyone)
    monkeypatch.setattr(sites_api, "proposal_visibility_filter", _only_for("admin-bypass"))

    result = run.run_audit(factory, persist=False)

    assert not [b for b in result["breaches"] if b["surface"] == run.SITES and b["served_status"] is None]
    served = {b["reason"] for b in _breaches(result) if b["served_leak"]}
    assert "site_member_served:not_visible" in served
    assert "site_member_served:web_detail" in served
    assert w["bridge"].public_id not in str(result["breaches"])  # breach rows carry the site's id


def _only_for(tier: str) -> Any:
    from services.api.visibility import proposal_visibility_filter as real

    def predicate(entitlement: str = "public", now: Any = None) -> Any:
        return [Proposal.publish_state == "public"] if entitlement == tier else real(entitlement, now)

    return predicate


def test_a_flagged_site_must_answer_404_and_a_regression_serving_it_is_found(
    db: Session, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    w = seed(db)
    w["site"].review_flag = "oversize"
    db.commit()

    clean = run.run_audit(factory, persist=False)
    assert clean["m11"] == 0, clean["breaches"]
    hidden = [
        c for c in clean["served"]["checks"] if c["surface"] == run.SITES and c["kind"] == "must_be_hidden"
    ]
    assert {c["path"] for c in hidden} == {
        f"/v1/sites/{w['site'].public_id}",
        f"/sites/{w['site'].public_id}",
    }
    assert all(c["status"] == 404 for c in hidden)

    monkeypatch.setattr(sites_api, "_servable", lambda site: True)
    result = run.run_audit(factory, persist=False)
    found = _breaches(result, "site_hidden_printed:oversize")
    assert [b["public_id"] for b in found] == [w["site"].public_id]
    assert found[0]["served_leak"] is True
    assert _breaches(result, "site_embed_printed:proposal_embed:site_not_servable")
