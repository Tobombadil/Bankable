"""Grid interconnection points (owner decision 2026-09-28; docs/21 §3.24; docs/25 §1): the loader's
linking pass, `GET /v1/interconnection-points[/{id}]`, the proposal-detail embed and the
`interconnection_point_id` proposal filter.

The load-bearing property is the aggregate rule: totals are computed per request over the proposals
the caller's tier may see, so a hidden proposal's capacity never reaches a public number, and a
point whose only proposals are hidden -- or whose naming register is gated -- does not exist on that
tier. Each test below builds the smallest store that could break one half of that.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from typing import Any

import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.conftest import make_attribution_licence, make_event, make_open_licence, make_public_source
from services.api.ratelimit import default_limiter
from services.api.visibility import interconnection_point_visible
from services.db.models import Event, InterconnectionPoint, Licence, Proposal, ProposalSource, Source
from services.ids import public_id, slugify
from services.ingest.interconnection import KEY_RULE_VERSION, link_all_points, link_source_points
from tests.conftest import login, make_account, make_user

UTC = dt.UTC
NOW = dt.datetime.now(UTC)
SPEC = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] / "api" / "openapi.yaml").read_text())


def assert_valid(schema_name: str, instance: object) -> None:
    schema = SPEC["components"]["schemas"][schema_name]
    resolver = jsonschema.validators.RefResolver.from_schema(SPEC)
    validator = jsonschema.validators.validator_for(schema)(schema, resolver=resolver)
    errors = sorted(validator.iter_errors(instance), key=str)
    assert not errors, "\n".join(f"{schema_name}: {e.message} at {list(e.absolute_path)}" for e in errors)


def _restricted_licence(db: Session) -> Licence:
    lic = Licence(
        id="restricted-lic",
        name="Restricted",
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
    return lic


def _source(db: Session, licence: Licence, id_: str, publish_state: str = "public") -> Source:
    src = make_public_source(db, licence, id_=id_)
    src.publish_state = publish_state
    db.flush()
    return src


def _proposal(
    db: Session,
    src: Source,
    key: str,
    *,
    poi: str | None,
    lifecycle: str = "filed",
    technology: str | None = "solar_pv",
    mw: float | None = 100.0,
    publish_state: str = "public",
    public_at: dt.datetime | None = None,
    jurisdiction: str = "US-TX",
    iso: str | None = "ERCOT",
    field: str = "Interconnection Location",
) -> Proposal:
    prop = Proposal(
        public_id="",
        slug="",
        kind="generation",
        name_canonical=f"Project {key}",
        jurisdiction=jurisdiction,
        iso=iso,
        lifecycle_state=lifecycle,
        capacity_mw=mw,
        technology=technology,
        publish_state=publish_state,
        published_at=NOW - dt.timedelta(days=20),
        public_at=public_at or (NOW - dt.timedelta(days=1)),
        min_reuse_class=src.licence.reuse_class,
        source_count=1,
    )
    db.add(prop)
    db.flush()
    prop.public_id = public_id("prop", prop.id)
    prop.slug = f"{slugify(prop.name_canonical)}-{key}"
    db.add(
        ProposalSource(
            proposal_id=prop.id,
            source_id=src.id,
            source_record_id=f"Q-{key}",
            source_url=f"{src.url}#{key}",
            retrieved_at=NOW - dt.timedelta(days=1),
            licence_id=src.licence_id,
            raw={"Queue ID": key, **({field: poi} if poi is not None else {})},
            first_seen=NOW - dt.timedelta(days=30),
            last_seen=NOW - dt.timedelta(days=1),
        )
    )
    db.flush()
    return prop


def _point(db: Session, prop: Proposal) -> InterconnectionPoint:
    db.refresh(prop)
    point = db.get(InterconnectionPoint, prop.interconnection_point_id)
    assert point is not None
    return point


def _get(client: TestClient, url: str, **params: Any) -> Any:
    default_limiter.reset()
    return client.get(url, params=params)


@pytest.fixture()
def world(db: Session) -> dict[str, Any]:
    """ERCOT-like open register with three points, a second register naming "the same" bus, and
    every way a proposal or point can be hidden."""
    open_lic = make_open_licence(db)
    attr_lic = make_attribution_licence(db)
    ercot = _source(db, open_lic, "us.test.ercot")
    caiso = _source(db, attr_lic, "us.test.caiso")
    staged = _source(db, open_lic, "us.test.staged", publish_state="api_only")
    pjm = _source(db, _restricted_licence(db), "us.test.pjm")

    props = {
        # Bearkat 345: three spellings, one bus. Active 300 MW solar+wind, 50 MW withdrawn, 20 built,
        # plus an unpublished 999 MW row and a not-yet-public 700 MW row that must not count publicly.
        "b1": _proposal(db, ercot, "b1", poi="59903 Bearkat 345kV", mw=200.0),
        "b2": _proposal(db, ercot, "b2", poi="59903 BEARKAT 345 kV", technology="wind", lifecycle="studied"),
        "b3": _proposal(
            db, ercot, "b3", poi="Bearkat Substation 345kV (#59903)", lifecycle="withdrawn", mw=50.0
        ),
        "b4": _proposal(db, ercot, "b4", poi="59903 Bearkat 345kV", lifecycle="built", mw=20.0),
        "b5": _proposal(db, ercot, "b5", poi="59903 Bearkat 345kV", mw=999.0, publish_state="unpublished"),
        "b6": _proposal(
            db, ercot, "b6", poi="59903 Bearkat 345kV", mw=700.0, public_at=NOW + dt.timedelta(days=5)
        ),
        "b7": _proposal(
            db, ercot, "b7", poi="59903 Bearkat 345kV", mw=None, lifecycle="unknown", technology=None
        ),
        # A line tap spelled both ways round.
        "t1": _proposal(db, ercot, "t1", poi="Tap 138kV 3127 Trinidad - 212 Winkler", mw=150.0),
        "t2": _proposal(db, ercot, "t2", poi="tap 138kV 212 Winkler - 3127 Trinidad", mw=10.0),
        # A point whose only proposal is unpublished: must not exist publicly.
        "h1": _proposal(db, ercot, "h1", poi="8140 Joslin 138kV", mw=500.0, publish_state="unpublished"),
        # A different register naming Bearkat too: never merged into ERCOT's point.
        "c1": _proposal(db, caiso, "c1", poi="Bearkat 345 kV", mw=80.0, iso="CAISO", jurisdiction="US-CA"),
        # A register on the Pro surface only.
        "s1": _proposal(db, staged, "s1", poi="Staged Sub 230kV", mw=60.0),
        # No POI at all, and a placeholder.
        "n1": _proposal(db, ercot, "n1", poi=None),
        "n2": _proposal(db, ercot, "n2", poi="TBD"),
    }
    # A PJM-named point: the proposal row exists (a store can hold it; the loader never would publish
    # it) and is made visible on its own to prove the point's register clause alone hides the point.
    pjm_prop = _proposal(db, ercot, "p1", poi="Placeholder", mw=40.0)
    results = link_all_points(db)
    pjm_point = InterconnectionPoint(
        public_id="",
        operator="PJM",
        name_display="Gated Sub 500kV",
        name_key="sub:gated|500",
        key_rule=KEY_RULE_VERSION,
        kind="substation",
        source_id=pjm.id,
        source_url=pjm.url,
        retrieved_at=NOW,
        licence_id=pjm.licence_id,
    )
    db.add(pjm_point)
    db.flush()
    pjm_point.public_id = public_id("poi", pjm_point.id)
    pjm_prop.interconnection_point_id = pjm_point.id
    db.commit()
    return {
        "props": props,
        "pjm_point": pjm_point,
        "results": results,
        "sources": (ercot, caiso, staged, pjm),
    }


# --------------------------------------------------------------------------------------- loading
def test_linking_groups_spellings_and_never_crosses_registers(db: Session, world: dict[str, Any]) -> None:
    props = world["props"]
    bearkat = _point(db, props["b1"])
    assert {_point(db, props[k]).id for k in ("b2", "b3", "b4", "b5", "b6", "b7")} == {bearkat.id}
    assert bearkat.name_display == "59903 Bearkat 345kV"  # the most common spelling
    assert (bearkat.kind, float(bearkat.voltage_kv), bearkat.bus_number) == ("substation", 345.0, "59903")
    assert (bearkat.operator, bearkat.jurisdiction, bearkat.source_id) == ("ERCOT", "US-TX", "us.test.ercot")
    assert _point(db, props["t1"]).id == _point(db, props["t2"]).id
    assert _point(db, props["t1"]).kind == "line_tap"
    caiso_point = _point(db, props["c1"])
    assert caiso_point.id != bearkat.id and caiso_point.operator == "CAISO"
    for key in ("n1", "n2"):
        db.refresh(props[key])
        assert props[key].interconnection_point_id is None
    ercot_result = next(r for r in world["results"] if r.source_id == "us.test.ercot")
    assert ercot_result.links_seen == 13 and ercot_result.with_poi == 12 and ercot_result.unparsed == 1
    assert "proposals linked" in ercot_result.summary()


def test_linking_is_idempotent_and_follows_a_revised_poi(db: Session, world: dict[str, Any]) -> None:
    ercot = world["sources"][0]
    before = {p.name_key: p.id for p in db.scalars(select(InterconnectionPoint))}
    again = link_source_points(db, ercot)
    assert again.points_created == 0
    assert {p.name_key: p.id for p in db.scalars(select(InterconnectionPoint))} == before
    # The register revises one row's POI: the proposal moves, the old point keeps the others.
    link = db.scalar(select(ProposalSource).where(ProposalSource.source_record_id == "Q-b2"))
    assert link is not None
    link.raw = {"Interconnection Location": "8437 Rincon 138kV"}
    db.flush()
    moved = link_source_points(db, ercot)
    assert moved.points_created == 1
    assert _point(db, world["props"]["b2"]).name_display == "8437 Rincon 138kV"
    assert _point(db, world["props"]["b1"]).name_key == "sub:bearkat|345|b59903"


def test_a_point_from_another_register_is_kept(db: Session, world: dict[str, Any]) -> None:
    """A proposal linked from register A is not re-pointed by register B naming its POI too."""
    caiso = world["sources"][1]
    prop = world["props"]["t1"]
    original = _point(db, prop).id
    db.add(
        ProposalSource(
            proposal_id=prop.id,
            source_id=caiso.id,
            source_record_id="Q-t1-caiso",
            source_url=caiso.url,
            retrieved_at=NOW,
            licence_id=caiso.licence_id,
            raw={"Interconnection Location": "Somewhere Else 500kV"},
            first_seen=NOW,
            last_seen=NOW,
        )
    )
    db.flush()
    link_source_points(db, caiso)
    assert _point(db, prop).id == original


def test_connection_site_field_is_one_site(db: Session) -> None:
    lic = make_attribution_licence(db)
    neso = _source(db, lic, "gb.test.neso")
    prop = _proposal(
        db,
        neso,
        "g1",
        poi="Sussex and Romney Connection Node A 400kV Substation",
        field="Connection Site",
        iso="NESO",
        jurisdiction="GB",
    )
    result = link_source_points(db, neso)
    assert result.linked == 1
    point = _point(db, prop)
    assert (point.kind, point.operator, point.jurisdiction) == ("substation", "NESO", "GB")


def test_a_source_with_no_links_links_nothing(db: Session) -> None:
    src = _source(db, make_open_licence(db), "us.test.empty")
    assert link_source_points(db, src).links_seen == 0


def test_the_loader_links_points_on_a_proposal_load(db: Session) -> None:
    """`load_dataframe` runs the pass itself, so every load path (dev_up, CLI, scheduler) fills it."""
    import pandas as pd

    from services.ingest.loader import load_dataframe

    src = _source(db, make_open_licence(db), "us.test.loaded")
    frame = pd.DataFrame(
        [
            {
                "record_id": f"us.test.loaded:{i}",
                "source_id": src.id,
                "source_record_id": str(i),
                "source_url": src.url,
                "retrieved_at": NOW.isoformat(),
                "kind": "generation",
                "name_canonical": f"Loaded {i}",
                "technology": "solar_pv",
                "capacity_mw": 10.0 * (i + 1),
                "state": "TX",
                "county": "Travis",
                "iso": "ERCOT",
                "lifecycle_state": "filed",
                "raw": {"Interconnection Location": poi},
            }
            for i, poi in enumerate(
                ["59903 Bearkat 345kV", "59903 BEARKAT 345 kV", "Cortland - Fenner 115kV"]
            )
        ]
    )
    result = load_dataframe(db, src, "proposal", frame, None)
    assert result.interconnection is not None
    assert (result.interconnection.linked, result.interconnection.points_created) == (3, 2)


# ------------------------------------------------------------------------------------------ list
def test_list_totals_count_only_visible_proposals(client: TestClient, world: dict[str, Any]) -> None:
    resp = _get(client, "/v1/interconnection-points", include="count")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert_valid("InterconnectionPointListResponse", body)
    names = [p["name"] for p in body["data"]]
    # Sorted by active MW, largest first: Bearkat 300, Trinidad-Winkler 160, CAISO Bearkat 80.
    assert names == ["59903 Bearkat 345kV", "Tap 138kV 3127 Trinidad - 212 Winkler", "Bearkat 345 kV"]
    assert body["meta"]["total"] == 3
    bearkat = body["data"][0]
    assert bearkat["totals"] == {
        "proposal_count": 5,
        "active_count": 2,
        "active_mw": 300.0,
        "withdrawn_count": 1,
        "withdrawn_mw": 50.0,
        "built_count": 1,
        "built_mw": 20.0,
        "other_count": 1,
        "other_mw": 0.0,
        "by_technology": [
            {"technology": "solar_pv", "count": 3, "active_count": 1, "active_mw": 200.0},
            {"technology": "wind", "count": 1, "active_count": 1, "active_mw": 100.0},
            {"technology": "unknown", "count": 1, "active_count": 0, "active_mw": 0.0},
        ],
    }
    assert (
        bearkat["kind"] == "substation"
        and bearkat["voltage_kv"] == 345.0
        and bearkat["bus_number"] == "59903"
    )
    assert bearkat["provenance"][0]["source_id"] == "us.test.ercot"
    assert bearkat["substation_asset"] is None
    # Hidden: the unpublished-only Joslin point, the Pro-surface register, the PJM-named point.
    assert not {"8140 Joslin 138kV", "Staged Sub 230kV", "Gated Sub 500kV"} & set(names)
    assert {row["source_id"] for row in body["licence_summary"]["sources"]} == {
        "us.test.ercot",
        "us.test.caiso",
    }


def test_pro_sees_the_api_only_register_and_not_yet_public_capacity(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    account = make_account(db, entitlement="pro", name="Pro Points")
    login(client, db, make_user(db, account, email="pro-points@example.com"))
    body = _get(client, "/v1/interconnection-points").json()
    by_name = {p["name"]: p for p in body["data"]}
    assert "Staged Sub 230kV" in by_name
    assert by_name["59903 Bearkat 345kV"]["totals"]["active_mw"] == 1000.0  # + the 700 MW not yet public
    # Unpublished and restricted stay dark on every tier.
    assert "8140 Joslin 138kV" not in by_name and "Gated Sub 500kV" not in by_name


def test_list_filters_and_sorts(client: TestClient, world: dict[str, Any]) -> None:
    def names(**params: Any) -> list[str]:
        resp = _get(client, "/v1/interconnection-points", **params)
        assert resp.status_code == 200, resp.text
        return [p["name"] for p in resp.json()["data"]]

    assert names(iso="CAISO") == ["Bearkat 345 kV"]
    assert names(jurisdiction="US-CA,GB") == ["Bearkat 345 kV"]
    assert names(kind="line_tap") == ["Tap 138kV 3127 Trinidad - 212 Winkler"]
    assert names(min_active_mw="160") == ["59903 Bearkat 345kV", "Tap 138kV 3127 Trinidad - 212 Winkler"]
    assert names(q="bearkat") == ["59903 Bearkat 345kV", "Bearkat 345 kV"]
    assert names(q="59903") == ["59903 Bearkat 345kV"]
    assert names(sort="name") == [
        "59903 Bearkat 345kV",
        "Bearkat 345 kV",
        "Tap 138kV 3127 Trinidad - 212 Winkler",
    ]
    assert names(sort="-voltage_kv")[-1] == "Tap 138kV 3127 Trinidad - 212 Winkler"
    for bad in (
        {"kind": "switchyard"},
        {"sort": "capacity"},
        {"min_active_mw": "lots"},
        {"include": "events"},
    ):
        resp = _get(client, "/v1/interconnection-points", **bad)
        assert resp.status_code == 400 and resp.json()["code"] == "validation_error", bad
    assert _get(client, "/v1/interconnection-points", cursor="not-a-cursor").status_code == 400


@pytest.mark.parametrize("sort", ["-active_mw", "active_mw", "name", "-voltage_kv", "voltage_kv"])
def test_cursor_paging_walks_every_point_once(client: TestClient, world: dict[str, Any], sort: str) -> None:
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 1, "sort": sort}
        if cursor:
            params["cursor"] = cursor
        body = _get(client, "/v1/interconnection-points", **params).json()
        seen.extend(p["public_id"] for p in body["data"])
        cursor = body["page"]["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 3


def test_a_cursor_with_a_bad_tiebreaker_is_a_400(client: TestClient, world: dict[str, Any]) -> None:
    from services.api.pagination import encode_cursor

    resp = _get(client, "/v1/interconnection-points", cursor=encode_cursor(1.0, "not-a-uuid"))
    assert resp.status_code == 400 and resp.json()["code"] == "invalid_cursor"


def test_null_sort_values_page_last(client: TestClient, db: Session, world: dict[str, Any]) -> None:
    """A point with no stated voltage sorts after every voltage in both directions and pages."""
    ercot = world["sources"][0]
    _proposal(db, ercot, "nv", poi="Cwm Rheidol")
    link_source_points(db, ercot)
    db.commit()
    for sort in ("voltage_kv", "-voltage_kv"):
        seen: list[str] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 1, "sort": sort}
            if cursor:
                params["cursor"] = cursor
            body = _get(client, "/v1/interconnection-points", **params).json()
            seen.extend(p["name"] for p in body["data"])
            cursor = body["page"]["next_cursor"]
            if not cursor:
                break
        assert seen[-1] == "Cwm Rheidol" and len(seen) == 4


# ---------------------------------------------------------------------------------------- detail
def test_detail_lists_visible_proposals_and_404s_hidden_points(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    props = world["props"]
    bearkat = _point(db, props["b1"])
    resp = _get(client, f"/v1/interconnection-points/{bearkat.public_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert_valid("InterconnectionPointDetailResponse", body)
    listed = [p["public_id"] for p in body["data"]["proposals"]]
    assert listed == [
        props[k].public_id for k in ("b1", "b2", "b4", "b7", "b3")
    ]  # active, built, other, withdrawn
    assert props["b5"].public_id not in listed and props["b6"].public_id not in listed
    assert body["data"]["proposals_truncated"] is False
    assert body["data"]["totals"]["active_mw"] == 300.0
    unknown = _get(client, "/v1/interconnection-points/poi_0000000000")
    for hidden in (_point(db, props["h1"]), _point(db, props["s1"]), world["pjm_point"]):
        resp = _get(client, f"/v1/interconnection-points/{hidden.public_id}")
        assert resp.status_code == 404
        assert resp.json()["code"] == unknown.json()["code"] == "not_found"
    assert _get(client, f"/v1/interconnection-points/{bearkat.public_id}", include="count").status_code == 400


def test_the_detail_caps_its_proposal_list(
    client: TestClient, db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.api import interconnection_points

    monkeypatch.setattr(interconnection_points, "DETAIL_PROPOSAL_CAP", 2)
    bearkat = _point(db, world["props"]["b1"])
    body = _get(client, f"/v1/interconnection-points/{bearkat.public_id}").json()
    assert len(body["data"]["proposals"]) == 2 and body["data"]["proposals_truncated"] is True
    assert body["data"]["totals"]["proposal_count"] == 5


# ------------------------------------------------------------------------- proposal detail, filter
def test_proposal_detail_embeds_the_point_with_public_totals(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    props = world["props"]
    body = _get(client, f"/v1/proposals/{props['b1'].public_id}").json()
    assert_valid("ProposalDetailResponse", body)
    embed = body["data"]["interconnection_point"]
    assert embed["public_id"] == _point(db, props["b1"]).public_id
    assert (embed["active_mw"], embed["active_count"], embed["proposal_count"]) == (300.0, 2, 5)
    assert embed["url"].endswith(f"/interconnection-points/{embed['public_id']}")
    assert (
        _get(client, f"/v1/proposals/{props['n1'].public_id}").json()["data"]["interconnection_point"] is None
    )
    # The proposal is visible; the register that named its point is not.
    gated = db.scalar(select(Proposal).where(Proposal.interconnection_point_id == world["pjm_point"].id))
    assert gated is not None
    assert _get(client, f"/v1/proposals/{gated.public_id}").json()["data"]["interconnection_point"] is None


def test_the_proposal_list_carries_the_same_point_as_the_detail(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    """Expert review 2026-10-07: an analyst screening a list needs the point per row. Same embed,
    same gate (a point named by a register the tier may not see is `null`)."""
    body = _get(client, "/v1/proposals", limit="200").json()
    assert_valid("ProposalListResponse", body)
    listed = {row["public_id"]: row["interconnection_point"] for row in body["data"]}
    for pid in listed:
        detail = _get(client, f"/v1/proposals/{pid}").json()["data"]["interconnection_point"]
        assert listed[pid] == detail, pid
    assert any(v is not None for v in listed.values()) and any(v is None for v in listed.values())


def test_the_proposal_filter_is_no_oracle_for_a_gated_point(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    props = world["props"]
    bearkat = _point(db, props["b1"]).public_id
    ids = {
        p["public_id"] for p in _get(client, "/v1/proposals", interconnection_point_id=bearkat).json()["data"]
    }
    assert ids == {props[k].public_id for k in ("b1", "b2", "b3", "b4", "b7")}
    for path, extra in (
        ("/v1/proposals", {}),
        ("/v1/proposals/geo", {"bbox": "-180,-90,180,90", "zoom": "3"}),
        ("/feeds/proposals.json", {}),
    ):
        gated = _get(client, path, interconnection_point_id=world["pjm_point"].public_id, **extra)
        unknown = _get(client, path, interconnection_point_id="poi_0000000000", **extra)
        assert gated.status_code == unknown.status_code == 200
        a, b = gated.json(), unknown.json()
        for body in (a, b):
            body.pop("meta", None)
            body.pop("feed_url", None)
            body.pop("home_page_url", None)
        assert a == b, path


def test_python_twin_matches_the_register_clauses(db: Session, world: dict[str, Any]) -> None:
    props = world["props"]
    assert interconnection_point_visible(_point(db, props["b1"]))
    assert not interconnection_point_visible(_point(db, props["s1"]))
    assert interconnection_point_visible(_point(db, props["s1"]), "pro")
    assert not interconnection_point_visible(world["pjm_point"], "pro")
    # A point whose own licence is gated even though its register's is not.
    point = _point(db, props["t1"])
    point.licence_id = "restricted-lic"
    db.flush()
    db.refresh(point)
    assert not interconnection_point_visible(point)


def test_the_bulk_stream_carries_the_point_as_the_detail_does(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    """The bulk stream's contract is the detail shape; the point travels only when its register's
    licence allows API redistribution, the rule every other field of a bulk record follows."""
    import json

    from tests.conftest import make_api_key

    account = make_account(db, entitlement="api", name="Bulk Points")
    user = make_user(db, account, email="bulk-points@example.com")
    _key, secret = make_api_key(db, account, user, scopes=["read:live", "read:bulk"])
    db.commit()
    headers = {"Authorization": f"Bearer {secret}"}

    def stream() -> dict[str, dict[str, Any]]:
        default_limiter.reset()
        resp = client.get("/v1/bulk/proposals", headers=headers)
        assert resp.status_code == 200, resp.text
        lines = [json.loads(line) for line in resp.iter_lines() if line][1:]
        return {line["public_id"]: line for line in lines}

    records = stream()
    b1 = world["props"]["b1"].public_id
    default_limiter.reset()
    detail = client.get(f"/v1/proposals/{b1}", headers=headers).json()["data"]
    assert records[b1]["interconnection_point"] == detail["interconnection_point"] is not None
    assert records[b1]["interconnection_point"]["active_mw"] == 1000.0  # api tier: live, not-yet-public too
    no_api = make_open_licence(db, id_="open-no-api")
    no_api.allows_api_redistribution = False
    point = _point(db, world["props"]["b1"])
    point.licence_id = no_api.id
    db.commit()
    assert stream()[b1]["interconnection_point"] is None
    default_limiter.reset()
    assert (
        client.get(f"/v1/proposals/{b1}", headers=headers).json()["data"]["interconnection_point"] is not None
    )


# ------------------------------------------------------------------------ recent changes (lane H2)
def _change(
    db: Session,
    prop: Proposal,
    src: Source,
    event_type: str,
    *,
    days_ago: float,
    public_at: dt.datetime | None = None,
    after: dict[str, Any] | None = None,
) -> Event:
    event = make_event(db, prop, src, event_type=event_type, public_at=public_at)
    event.observed_at = NOW - dt.timedelta(days=days_ago)
    event.idempotency_key = f"{event.idempotency_key}:{prop.public_id}:{days_ago}"
    if after is not None:
        event.after = after
    db.flush()
    return event


def test_recent_changes_list_the_visible_proposals_queue_events_newest_first(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    """The point's own proposals' change events, under the events endpoints' visibility: an event of
    an unpublished or not-yet-public proposal, a not-yet-public event, an event from a gated source,
    a field-level edit and another point's event are all absent."""
    props = world["props"]
    ercot, caiso, _staged, pjm = world["sources"]
    created = _change(db, props["b1"], ercot, "created", days_ago=9, after={"lifecycle_state": "filed"})
    status = _change(
        db, props["b2"], ercot, "status_change", days_ago=3, after={"lifecycle_state": "studied"}
    )
    withdrawn = _change(
        db, props["b3"], ercot, "withdrawn", days_ago=5, after={"lifecycle_state": "withdrawn"}
    )
    hidden = [
        _change(db, props["b5"], ercot, "status_change", days_ago=1),  # unpublished proposal
        _change(db, props["b6"], ercot, "created", days_ago=1),  # proposal not yet public
        _change(db, props["b1"], ercot, "status_change", days_ago=1, public_at=NOW + dt.timedelta(days=2)),
        _change(db, props["b1"], pjm, "status_change", days_ago=1),  # the event's own source is gated
        _change(db, props["b1"], ercot, "capacity_changed", days_ago=1),  # not queue news
        _change(db, props["c1"], caiso, "created", days_ago=1),  # another register's point
    ]
    db.commit()

    body = _get(client, f"/v1/interconnection-points/{_point(db, props['b1']).public_id}").json()

    assert_valid("InterconnectionPointDetailResponse", body)
    changes = body["data"]["recent_changes"]
    assert [c["id"] for c in changes] == [
        public_id("evt", e.id) for e in (status, withdrawn, created)
    ]  # newest observation first
    assert not {public_id("evt", e.id) for e in hidden} & {c["id"] for c in changes}
    listed = {p["public_id"] for p in body["data"]["proposals"]}
    assert {c["subject"]["public_id"] for c in changes} <= listed
    first = changes[0]
    assert first["event_type"] == "status_change" and first["after"] == {"lifecycle_state": "studied"}
    assert first["subject"]["url"].endswith(f"/proposals/{props['b2'].slug}")
    assert first["provenance"]["source_id"] == ercot.id
    assert ercot.id in {row["source_id"] for row in body["licence_summary"]["sources"]}
    # The other register's point lists its own event only.
    caiso_changes = _get(client, f"/v1/interconnection-points/{_point(db, props['c1']).public_id}").json()
    assert [c["subject"]["public_id"] for c in caiso_changes["data"]["recent_changes"]] == [
        props["c1"].public_id
    ]


def test_recent_changes_are_capped_and_empty_when_nothing_is_recorded(
    client: TestClient, db: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.api import interconnection_points

    props = world["props"]
    ercot = world["sources"][0]
    tap = _point(db, props["t1"]).public_id
    assert _get(client, f"/v1/interconnection-points/{tap}").json()["data"]["recent_changes"] == []
    for n in range(4):
        _change(db, props["b1"], ercot, "status_change", days_ago=10 - n)
    db.commit()
    monkeypatch.setattr(interconnection_points, "RECENT_CHANGES_LIMIT", 3)
    changes = _get(client, f"/v1/interconnection-points/{_point(db, props['b1']).public_id}").json()["data"][
        "recent_changes"
    ]
    assert len(changes) == 3
    assert [c["observed_at"] for c in changes] == sorted((c["observed_at"] for c in changes), reverse=True)


def test_recent_point_changes_batches_and_caps_per_point(db: Session, world: dict[str, Any]) -> None:
    from services.api.interconnection_points import recent_point_changes

    props = world["props"]
    ercot, caiso = world["sources"][:2]
    for n in range(3):
        _change(db, props["b1"], ercot, "status_change", days_ago=10 - n)
    _change(db, props["t1"], ercot, "created", days_ago=4)
    _change(db, props["c1"], caiso, "withdrawn", days_ago=2)
    db.commit()
    bearkat, tap, caiso_pt, joslin = (_point(db, props[k]) for k in ("b1", "t1", "c1", "h1"))

    got = recent_point_changes(db, [bearkat.id, tap.id, caiso_pt.id, joslin.id], "public", limit=2)

    assert [len(got[p.id]) for p in (bearkat, tap, caiso_pt, joslin)] == [2, 1, 1, 0]
    assert recent_point_changes(db, [], "public") == {}


def test_recent_changes_list_a_departure_from_the_register(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    """A project no longer in its register's report (`delisted`, docs/21 §7.3) frees queue space at
    the point, so "Recent changes at this point" lists it, headlined with its own sentence."""
    props = world["props"]
    ercot = world["sources"][0]
    gone = _change(
        db,
        props["b3"],
        ercot,
        "delisted",
        days_ago=2,
        after={"source_id": ercot.id, "register_name": "ERCOT", "reason": "not stated"},
    )
    gone.reason = "No longer in ERCOT's report (reason not stated)"
    db.commit()

    body = _get(client, f"/v1/interconnection-points/{_point(db, props['b1']).public_id}").json()

    assert_valid("InterconnectionPointDetailResponse", body)
    changes = body["data"]["recent_changes"]
    assert [c["id"] for c in changes] == [public_id("evt", gone.id)]
    assert changes[0]["event_type"] == "delisted"
    assert (
        changes[0]["headline"]
        == f"{props['b3'].name_canonical}: No longer in ERCOT's report (reason not stated)"
    )
