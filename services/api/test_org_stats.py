"""`GET /v1/organizations` with figures (2026-10-22, lane org-stats): `organization_stats`, its
refresh and the list's search, filters and sorts.

The contract held here, on the portfolio's fixture group (`services/api/test_org_portfolio.py`'s
`Store`: two subsidiaries, a grandchild, a taken-down subsidiary, an unrelated company, a
restricted register, a register not on the surface) plus proposals and opportunities around it:

- every figure equals the portfolio's or the pipeline's for the same organisation and scope, and is
  the total of the list query it names;
- a group counts an asset once, and never what a taken-down subsidiary holds;
- nothing a reader may not see is counted, and a gate that moves after a refresh withholds every
  figure until the next one;
- the sorts order by the figure with missing figures last in both directions, across pages; each
  filter selects exactly the rows its figure says; the search ignores case and punctuation, reads
  readable aliases only, and puts prefixes first;
- a second refresh writes nothing; the parameters are checked; the latency is within the list
  budget on a store larger than the real one.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import shutil
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import jsonschema
import pytest
import sqlalchemy as sa
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api import org_stats
from services.api.conftest import make_org, make_visible_opportunity, make_visible_proposal
from services.api.org_stats_refresh import refresh
from services.api.ratelimit import default_limiter
from services.api.test_org_portfolio import GATED_NAMES, Store
from services.api.visibility import PUBLISHABLE_REUSE_CLASSES
from services.db.models import (
    Asset,
    AssetOwner,
    Opportunity,
    Organization,
    OrganizationAlias,
    OrganizationStats,
    Proposal,
    ProposalSource,
)
from services.ids import public_id
from tests.latency import median_seconds

UTC = dt.UTC
SPEC = yaml.safe_load((pathlib.Path(__file__).resolve().parents[2] / "api" / "openapi.yaml").read_text())
FIGURES = ("assets", "operating_mw", "owned_mw", "pipeline_count", "pipeline_mw", "rfps_issued")


def _validate(instance: Any) -> None:
    schema = SPEC["components"]["schemas"]["OrganizationListResponse"]
    resolver = jsonschema.validators.RefResolver.from_schema(SPEC)
    validator = jsonschema.validators.validator_for(schema)(schema, resolver=resolver)
    errors = sorted(validator.iter_errors(instance), key=str)
    assert not errors, "\n".join(f"{e.message} at {list(e.absolute_path)}" for e in errors)


def _get(client: TestClient, path: str = "/v1/organizations", **params: str) -> dict[str, Any]:
    default_limiter.reset()
    resp = client.get(path, params=params)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    if path == "/v1/organizations":
        _validate(body)
    return body


def _all(client: TestClient, **params: str) -> list[dict[str, Any]]:
    """Every row the list returns for `params`, every page."""
    rows: list[dict[str, Any]] = []
    cursor = None
    while True:
        body = _get(client, **{"limit": "200", **params, **({"cursor": cursor} if cursor else {})})
        rows.extend(body["data"])
        cursor = body["page"]["next_cursor"]
        if not cursor:
            return rows


def _by_name(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["name_canonical"]: r for r in rows}


@dataclass
class World:
    store: Store
    proposals: dict[str, Proposal]
    opportunities: dict[str, Opportunity]


def _proposal(
    db: Session,
    store: Store,
    suffix: str,
    sponsor: Organization,
    state: str,
    *,
    technology: str = "solar",
    jurisdiction: str = "US-TX",
    iso: str | None = None,
    kind: str = "generation",
    source: Any = None,
) -> Proposal:
    prop = make_visible_proposal(
        db,
        source or store.plants,
        public_id_suffix=suffix,
        sponsor=sponsor,
        lifecycle_state=state,
        technology=technology,
        jurisdiction=jurisdiction,
    )
    prop.iso, prop.kind = iso, kind
    db.flush()
    return prop


def _opportunity(
    db: Session, store: Store, suffix: str, issuer: Organization, status: str, source: Any = None
) -> Opportunity:
    opp = make_visible_opportunity(db, source or store.plants, public_id_suffix=suffix, status=status)
    opp.issuer_org_id = issuer.id
    db.flush()
    return opp


def build_world(db: Session) -> World:
    store = Store(db)
    p = {
        # Generation A: two active (ERCOT solar in Texas, SPP wind in Oklahoma), one withdrawn, one built.
        "a_solar": _proposal(db, store, "1", store.gen_a, "filed", iso="ERCOT"),
        "a_wind": _proposal(
            db, store, "2", store.gen_a, "permitted", technology="wind", jurisdiction="US-OK", iso="SPP"
        ),
        "a_withdrawn": _proposal(db, store, "3", store.gen_a, "withdrawn"),
        "a_built": _proposal(db, store, "4", store.gen_a, "built"),
        # The grandchild: an active large load filed under ERCOT's balancing-authority code. Counted,
        # its MW never summed, its grid read as ERCOT.
        "grand_load": _proposal(
            db, store, "5", store.grand, "under_construction", technology="load", kind="load", iso="ERCO"
        ),
        # What must lend nothing to the group: the taken-down subsidiary's, and one only a register
        # off the surface lists.
        "hidden_sub": _proposal(db, store, "6", store.hidden, "filed"),
        "staged_only": _proposal(db, store, "7", store.gen_b, "filed", source=store.staged_owners),
        # Generation B: one active project no register lists any more (counted, not active).
        "b_delisted": _proposal(db, store, "8", store.gen_b, "filed", jurisdiction="US-NM"),
        "other": _proposal(db, store, "9", store.other, "filed"),
    }
    link = db.scalar(select(ProposalSource).where(ProposalSource.proposal_id == p["b_delisted"].id))
    assert link is not None
    link.gone_at = dt.datetime(2026, 10, 1, tzinfo=UTC)
    o = {
        "root_open": _opportunity(db, store, "1", store.root, "open"),
        "b_closed": _opportunity(db, store, "2", store.gen_b, "closed"),
        "a_staged": _opportunity(db, store, "3", store.gen_a, "open", source=store.staged_owners),
    }
    db.commit()
    return World(store, p, o)


@pytest.fixture()
def world(db: Session) -> World:
    w = build_world(db)
    refresh(db)
    db.commit()
    org_stats.forget_gate_key()
    return w


def _stats(client: TestClient, org: Organization, scope: str = "self") -> dict[str, Any]:
    row = _by_name(_all(client, scope=scope))[org.name_canonical]
    stats: dict[str, Any] = row["stats"]
    return stats


# ------------------------------------------------------------------ equal to the endpoints
@pytest.mark.parametrize(("scope", "tree_scope"), [("self", "self"), ("group", "all")])
def test_every_figure_equals_the_portfolio_and_the_pipeline(
    client: TestClient, world: World, scope: str, tree_scope: str
) -> None:
    s = world.store
    rows = _by_name(_all(client, scope=scope))
    assert "Fleet Hidden Sub" not in rows  # a taken-down organisation is not listed
    for org in (s.root, s.gen_a, s.gen_b, s.grand, s.other):
        stats = rows[org.name_canonical]["stats"]
        portfolio = _get(client, f"/v1/organizations/{org.public_id}/portfolio", scope=tree_scope)["data"]
        pipeline = _get(client, f"/v1/organizations/{org.public_id}/pipeline", scope=tree_scope)["data"]
        roles = {e["role"]: e["assets"] for e in portfolio["by_role"]}
        totals = portfolio["totals"]
        withdrawn = sum(
            e["listed"]["records"] + e["not_listed"]["records"]
            for e in pipeline["by_lifecycle_state"]
            if e["lifecycle_state"] == "withdrawn"
        )
        assert (
            stats["organizations"],
            stats["assets"],
            stats["operated_assets"],
            stats["owned_assets"],
            stats["operating_mw"],
            stats["operating_mw_assets"],
            stats["owned_mw"],
            stats["owned_mw_assets"],
        ) == (
            portfolio["scope"]["organizations"],
            totals["assets"],
            roles.get("operator", 0),
            roles.get("owner", 0),
            totals["capacity_mw"],
            totals["capacity_assets"],
            totals["owned_mw"],
            totals["owned_mw_assets"],
        ), (org.name_canonical, scope)
        assert (
            stats["pipeline_count"],
            stats["pipeline_mw"],
            stats["pipeline_mw_records"],
            stats["proposals"],
            stats["withdrawn_count"],
        ) == (
            pipeline["active"]["records"],
            pipeline["active"]["capacity_mw"],
            pipeline["active"]["capacity_records"],
            pipeline["totals"]["records"],
            withdrawn,
        ), (org.name_canonical, scope)
        assert set(stats["isos"]) == {e["iso"] for e in pipeline["by_iso"] if e["iso"]}


def test_the_group_figures_the_fixture_was_built_for(client: TestClient, world: World) -> None:
    group = _stats(client, world.store.root, "group")
    assert group["organizations"] == 4  # root, two subsidiaries, the grandchild; not the hidden one
    assert (group["assets"], group["operating_mw"], group["owned_mw"]) == (6, 1900.0, 900.0)
    # Active: Generation A's two and the grandchild's load; its MW is demand, never summed.
    assert (group["pipeline_count"], group["pipeline_mw"], group["pipeline_mw_records"]) == (3, 203.0, 2)
    assert (group["withdrawn_count"], group["proposals"]) == (1, 6)
    assert (group["rfps_issued"], group["rfps_open"]) == (2, 1)
    assert group["isos"] == ["ERCOT", "SPP"]  # `ERCO` read as the operator it names
    assert group["states"] == ["US-AZ", "US-CA", "US-OK", "US-TX"]
    assert {"solar", "wind", "gas_cc", "load", "solar_pv"} <= set(group["technologies"])
    own = _stats(client, world.store.root)
    assert (own["organizations"], own["assets"], own["pipeline_count"], own["rfps_issued"]) == (1, 1, 0, 1)


def test_the_group_counts_an_asset_once(client: TestClient, world: World) -> None:
    s = world.store
    rows = _by_name(_all(client))
    members = [rows[o.name_canonical]["stats"]["assets"] for o in (s.root, s.gen_a, s.gen_b, s.grand)]
    # The shared plant is held by the parent and both subsidiaries; the solar farm by A and the
    # grandchild. Their own figures add to more than the group holds.
    assert sum(members) > _stats(client, s.root, "group")["assets"] == 6


def test_each_figure_is_the_total_of_the_list_query_it_names(client: TestClient, world: World) -> None:
    s = world.store
    for org, scope in ((s.root, "group"), (s.gen_a, "self"), (s.gen_b, "self")):
        stats = _stats(client, org, scope)
        checked = 0
        for figure, lq in stats["list_queries"].items():
            if lq["path"].endswith("/assets"):
                listed: dict[str, float | None] = {}
                cursor = None
                while True:
                    params = {**lq["query"], "limit": "200", **({"cursor": cursor} if cursor else {})}
                    body = _get(client, lq["path"], **params)
                    listed.update({r["public_id"]: r["capacity_mw"] for r in body["data"]})
                    cursor = body["page"]["next_cursor"]
                    if not cursor:
                        break
                if figure == "operating_mw":
                    mw = [v for v in listed.values() if v is not None]
                    assert stats[figure] == (round(sum(mw), 3) if mw else None), (org.name_canonical, figure)
                elif figure != "owned_mw":  # owned MW reads the shares; its list is the owner edges
                    assert stats[figure] == len(listed), (org.name_canonical, figure, lq)
            else:
                body = _get(client, lq["path"], **lq["query"], include="count", limit="200")
                if figure == "pipeline_mw":
                    mw = [r["capacity_mw"] for r in body["data"] if r["kind"] != "load" and r["capacity_mw"]]
                    assert stats[figure] == (round(sum(mw), 3) if mw else None), (org.name_canonical, lq)
                else:
                    assert stats[figure] == body["meta"]["total"], (org.name_canonical, figure, lq)
            checked += 1
        assert checked == 11


# -------------------------------------------------------------------------------- the gates
def test_what_a_reader_may_not_see_is_never_counted(client: TestClient, world: World) -> None:
    default_limiter.reset()
    resp = client.get("/v1/organizations", params={"scope": "group", "limit": "200"})
    for name in (*GATED_NAMES, "us.test.restricted_owners", "us.test.staged_owners"):
        assert name not in resp.text, name
    # Generation B: the gated plant (restricted and off-surface registers only) lends it nothing;
    # the project only an off-surface register lists is not counted, nor is A's staged RFP.
    gen_b = _stats(client, world.store.gen_b)
    assert (gen_b["assets"], gen_b["owned_assets"], gen_b["operated_assets"]) == (3, 1, 2)
    assert gen_b["proposals"] == 1 and gen_b["pipeline_count"] == 0  # delisted: counted, not active
    assert _stats(client, world.store.gen_a)["rfps_issued"] == 0


def test_a_takedown_withholds_every_figure_until_the_next_refresh(
    client: TestClient, world: World, db: Session
) -> None:
    before = _stats(client, world.store.gen_a)["pipeline_count"]
    prop = db.get(Proposal, world.proposals["a_solar"].id)
    assert prop is not None
    prop.publish_state = "unpublished"  # what the admin takedown writes; `published_at` stays
    db.commit()
    body = _get(client, limit="200", sort="-pipeline_count")
    assert body["meta"]["organization_stats"]["state"] == "stale"
    assert all(row["stats"] is None for row in body["data"])
    assert _get(client, has_pipeline="true")["data"] == []  # no figure, no match
    assert body["licence_summary"]["sources"] == []

    report = refresh(db)
    db.commit()
    assert report.gate_key_changed and report.written
    assert _get(client)["meta"]["organization_stats"]["state"] == "current"
    assert _stats(client, world.store.gen_a)["pipeline_count"] == before - 1


@pytest.mark.parametrize("change", ["source", "licence", "organization", "person"])
def test_any_gate_that_moves_withholds_the_figures(
    client: TestClient, world: World, db: Session, change: str
) -> None:
    s = world.store
    if change == "source":
        s.operators.publish_state = "ingest_only"
    elif change == "licence":
        s.open.reuse_class = "restricted"
    elif change == "organization":
        s.gen_b.publish_state = "unpublished"
    else:
        s.grand.personal_data = True
    db.commit()
    assert _get(client)["meta"]["organization_stats"]["state"] == "stale"
    refresh(db)
    db.commit()
    assert _get(client)["meta"]["organization_stats"]["state"] == "current"
    if change == "organization":
        # Generation B and everything it held leave the root group's figures.
        assert _stats(client, s.root, "group")["organizations"] == 3


def test_a_noncommercial_edge_counts_only_under_the_posture_that_publishes_it(
    client: TestClient, db: Session
) -> None:
    from services.api.conftest import make_asset, make_asset_owner, make_open_licence, make_public_source
    from services.api.test_org_portfolio import _licence, _source

    lic = make_open_licence(db)
    nc = _licence(db, "nc-lic", "noncommercial")
    plants = make_public_source(db, lic)
    owners = _source(db, nc, "us.test.nc_owners", "Noncommercial Owners")
    org = make_org(db, "Posture Power")
    plant = make_asset(db, plants, lic, source_asset_id="nc1", name="Posture Plant")
    make_asset_owner(db, plant, org, owners, nc, share_pct=100.0)
    db.commit()
    refresh(db)
    db.commit()
    published = "noncommercial" in PUBLISHABLE_REUSE_CLASSES
    assert _stats(client, org)["assets"] == (1 if published else 0)


def test_before_any_refresh_the_list_says_so_and_still_lists(client: TestClient, db: Session) -> None:
    build_world(db)
    body = _get(client, sort="-operating_mw", limit="200")
    assert body["meta"]["organization_stats"] == {"scope": "self", "tier": "public", "state": "missing"}
    assert len(body["data"]) == 5 and all(row["stats"] is None for row in body["data"])
    assert _get(client, has_assets="true")["data"] == []
    assert [r["name_canonical"] for r in _get(client, q="fleet gen")["data"]] == [
        "Fleet Generation A",
        "Fleet Generation B",
    ]


def test_a_paid_tier_reads_its_own_figures(world: World, db: Session) -> None:
    rows = {(r.organization_id, r.scope, r.tier) for r in db.scalars(select(OrganizationStats))}
    orgs = {o.id for o in (world.store.root, world.store.gen_a, world.store.other)}
    for org_id in orgs:
        assert {(org_id, scope, tier) for scope in ("self", "group") for tier in ("public", "pro")} <= rows
    assert org_stats.tier_for("api") == org_stats.tier_for("pro") == "pro"
    assert org_stats.tier_for("admin") == org_stats.tier_for("public") == "public"


# ------------------------------------------------------------------------- sorts and filters
def _figure(row: dict[str, Any], field: str) -> Any:
    return row["stats"][field] if row["stats"] else None


@pytest.mark.parametrize("field", FIGURES)
@pytest.mark.parametrize("scope", ["self", "group"])
def test_each_sort_orders_by_its_figure_with_missing_last_across_pages(
    client: TestClient, world: World, field: str, scope: str
) -> None:
    for direction in ("-", ""):
        whole = _all(client, sort=f"{direction}{field}", scope=scope)
        values = [_figure(r, field) for r in whole]
        present = [v for v in values if v is not None]
        assert values == present + [None] * (len(values) - len(present)), (field, direction)
        assert present == sorted(present, reverse=direction == "-"), (field, direction)
        paged = _all(client, sort=f"{direction}{field}", scope=scope, limit="2")
        assert [r["public_id"] for r in paged] == [r["public_id"] for r in whole]
    assert len(whole) == 5


def test_largest_first_puts_the_biggest_fleet_on_top(client: TestClient, world: World) -> None:
    top = _get(client, sort="-operating_mw", scope="group", limit="1")["data"][0]
    assert top["name_canonical"] == "Fleet Holdings" and top["stats"]["operating_mw"] == 1900.0
    names = [r["name_canonical"] for r in _all(client, sort="name")]
    assert names == [r["name_canonical"] for r in _all(client)] == sorted(names)


@pytest.mark.parametrize(
    ("params", "predicate"),
    [
        ({"state": "US-OK"}, lambda s: "US-OK" in s["states"]),
        ({"state": "us-ca,US-NM"}, lambda s: {"US-CA", "US-NM"} & set(s["states"])),
        ({"iso": "ERCO"}, lambda s: "ERCOT" in s["isos"]),
        ({"technology": "wind"}, lambda s: "wind" in s["technologies"]),
        ({"role": "operator"}, lambda s: s["operated_assets"] > 0),
        ({"role": "owner"}, lambda s: s["owned_assets"] > 0),
        ({"role": "sponsor"}, lambda s: s["proposals"] > 0),
        ({"role": "operator,sponsor"}, lambda s: s["operated_assets"] > 0 or s["proposals"] > 0),
        ({"has_assets": "true"}, lambda s: s["assets"] > 0),
        ({"has_assets": "false"}, lambda s: s["assets"] == 0),
        ({"has_pipeline": "true"}, lambda s: s["pipeline_count"] > 0),
        ({"has_pipeline": "false"}, lambda s: s["pipeline_count"] == 0),
        ({"min_operating_mw": "500"}, lambda s: (s["operating_mw"] or 0) >= 500),
        (
            {"has_assets": "true", "has_pipeline": "true"},
            lambda s: s["assets"] > 0 and s["pipeline_count"] > 0,
        ),
    ],
)
@pytest.mark.parametrize("scope", ["self", "group"])
def test_each_filter_selects_exactly_the_rows_its_figure_says(
    client: TestClient, world: World, params: dict[str, str], predicate: Any, scope: str
) -> None:
    every = _all(client, scope=scope)
    expected = {r["public_id"] for r in every if predicate(r["stats"])}
    got = _all(client, scope=scope, **params)
    assert {r["public_id"] for r in got} == expected, params
    total = _get(client, scope=scope, include="count", **params)["meta"]["total"]
    assert total == len(expected)


def test_the_group_scope_filters_on_the_group(client: TestClient, world: World) -> None:
    own = {r["name_canonical"] for r in _all(client, iso="SPP")}
    group = {r["name_canonical"] for r in _all(client, iso="SPP", scope="group")}
    assert own == {"Fleet Generation A"}
    assert group == {"Fleet Generation A", "Fleet Holdings"}


# ------------------------------------------------------------------------------------ search
def test_the_search_ignores_case_spacing_and_punctuation(
    client: TestClient, world: World, db: Session
) -> None:
    make_org(db, "A.E.S. Corp.")
    db.commit()
    names = lambda q: [r["name_canonical"] for r in _get(client, q=q, limit="200")["data"]]  # noqa: E731
    assert names("fleet-generation") == ["Fleet Generation A", "Fleet Generation B"]
    assert names("FLEETGEN") == ["Fleet Generation A", "Fleet Generation B"]
    assert names("aes corp") == names("AES") == ["A.E.S. Corp."]
    assert names("holdings") == ["Fleet Holdings"]
    assert names("%") == []  # a wildcard is a character searched for, not a pattern


def test_a_prefix_match_comes_first(client: TestClient, world: World, db: Session) -> None:
    make_org(db, "Acme Fleet Services")
    db.commit()
    names = [r["name_canonical"] for r in _get(client, q="fleet", limit="200")["data"]]
    assert names[-1] == "Acme Fleet Services"
    assert names[:-1] == sorted(names[:-1]) and all(n.startswith("Fleet") for n in names[:-1])
    # An explicit sort wins over relevance.
    by_size = _get(client, q="fleet", sort="-operating_mw", limit="1")["data"][0]
    assert by_size["name_canonical"] == "Fleet Generation B"  # 1,500 MW against A's 1,350


def test_only_an_alias_a_readable_register_spelled_is_searched(
    client: TestClient, world: World, db: Session
) -> None:
    s = world.store
    for spelling, source in (("FHI Power & Light", s.plants), ("Quiet Spelling Holdco", s.staged_owners)):
        db.add(
            OrganizationAlias(
                organization_id=s.other.id,
                alias=spelling,
                alias_normalised=spelling.lower(),
                kind="former_name",
                source_id=source.id,
                source_url=source.url,
                retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
                licence_id=source.licence_id,
            )
        )
    db.commit()
    assert [r["name_canonical"] for r in _get(client, q="fhi power and")["data"]] == []
    assert [r["name_canonical"] for r in _get(client, q="fhipower")["data"]] == ["Unrelated Utility"]
    assert _get(client, q="quiet spelling")["data"] == []


# ------------------------------------------------------------------------- refresh and meta
def test_a_second_refresh_writes_nothing_and_a_merged_company_leaves(world: World, db: Session) -> None:
    def table() -> list[tuple[Any, ...]]:
        return sorted(
            (str(r.organization_id), r.scope, r.tier, r.assets, r.pipeline_count, r.gate_key, r.computed_at)
            for r in db.scalars(select(OrganizationStats))
        )

    first = table()
    again = refresh(db)
    db.commit()
    assert (again.written, again.unchanged, again.gate_key_changed) == (0, again.rows, False)
    assert table() == first
    other = db.get(Organization, world.store.other.id)
    assert other is not None
    other.merged_into_id = world.store.root.id
    db.commit()
    merged = refresh(db)
    db.commit()
    assert (merged.deleted, merged.inserted, merged.updated) == (4, 0, 0)


def test_a_figure_that_changes_is_rewritten_and_the_row_says_when(world: World, db: Session) -> None:
    row = db.get(OrganizationStats, (world.store.gen_a.id, "self", "public"))
    assert row is not None
    computed = row.computed_at
    prop = db.get(Proposal, world.proposals["a_wind"].id)
    assert prop is not None
    prop.lifecycle_state = "withdrawn"
    db.commit()
    report = refresh(db, now=dt.datetime(2026, 10, 22, 12, tzinfo=UTC))
    db.commit()
    assert report.updated >= 2 and not report.gate_key_changed  # gen A's rows and the root group's
    db.expire_all()
    row = db.get(OrganizationStats, (world.store.gen_a.id, "self", "public"))
    assert row is not None and row.withdrawn_count == 2 and row.computed_at != computed


def test_the_licence_summary_credits_the_registers_behind_the_page(client: TestClient, world: World) -> None:
    body = _get(client, scope="group", limit="200")
    behind = {s["source_id"] for row in body["data"] for s in row["stats"]["sources"]}
    credited = {s["source_id"]: s for s in body["licence_summary"]["sources"]}
    assert set(credited) == behind
    assert behind == {"us.test.plants", "us.test.owner_shares", "us.test.emitters", "us.test.operators"}
    holdings = _by_name(body["data"])["Fleet Holdings"]["stats"]
    assert {s["source_id"] for s in holdings["sources"]} == behind
    assert credited["us.test.plants"]["record_count"] == 5  # organisations on the page it backs


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"scope": "all"}, "scope"),
        ({"role": "developer"}, "role"),
        ({"has_assets": "yes"}, "has_assets"),
        ({"has_pipeline": "1"}, "has_pipeline"),
        ({"min_operating_mw": "-1"}, "min_operating_mw"),
        ({"min_operating_mw": "lots"}, "min_operating_mw"),
        ({"sort": "-capacity_mw"}, "sort"),
        ({"state": "US-TX|"}, "state"),
        ({"iso": "ER%"}, "iso"),
    ],
)
def test_parameters_are_checked(client: TestClient, world: World, params: dict[str, str], field: str) -> None:
    resp = client.get("/v1/organizations", params=params)
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "validation_error" and resp.json()["errors"][0]["field"] == field


# -------------------------------------------------------------------------------- latency
#: docs/04 E-16's list budget (300 ms p95) with the margin the portfolio's budget test allows an
#: unknown runner. Measured 2026-10-22 on a copy of the 2026-10-20 store (8,454 companies): 38-83 ms
#: warm for a figure sort, a state filter and a name search; 151 ms with `include=count` and `q`.
BUDGET_S = 0.3
MARGIN = 3.0


def _large_store(db: Session, companies: int, *, prefix: str = "big") -> None:
    """`companies` organisations, each with two plants and an owner edge on each and two proposals,
    and a parent over every ten; written with bulk inserts, in foreign-key order."""
    from services.api.conftest import make_open_licence, make_public_source

    lic = make_open_licence(db, id_=f"{prefix}-lic")
    src = make_public_source(db, lic, id_=f"us.test.{prefix}")
    name = prefix.title()
    retrieved = dt.datetime(2026, 9, 1, tzinfo=UTC)
    now = dt.datetime.now(UTC)
    provenance = {"source_id": src.id, "source_url": src.url, "retrieved_at": retrieved, "licence_id": lic.id}
    stages: dict[Any, list[dict[str, Any]]] = {
        m: [] for m in (Organization, Asset, AssetOwner, Proposal, ProposalSource)
    }
    parent: uuid.UUID | None = None
    for i in range(companies):
        org_id = uuid.uuid4()
        stages[Organization].append(
            {
                "id": org_id,
                "public_id": public_id("org", org_id),
                "slug": f"{prefix}-company-{i}",
                "name_canonical": f"{name} Company {i}",
                "name_normalised": f"{prefix} company {i}",
                "type": "developer",
                "country": "US",
                "ids": {},
                "parent_org_id": parent if i % 10 else None,
                "personal_data": False,
                "personal_data_basis": "no_person_shape",
            }
        )
        if i % 10 == 0:
            parent = org_id
        for j in range(2):
            asset_id, prop_id = uuid.uuid4(), uuid.uuid4()
            stages[Asset].append(
                {
                    "id": asset_id,
                    "public_id": public_id("asset", asset_id),
                    "slug": f"{prefix}-plant-{i}-{j}",
                    "asset_type": "power_plant",
                    "source_asset_id": f"{prefix}{i}-{j}",
                    "name": f"{name} Plant {i}-{j}",
                    "status": "operating",
                    "technology": ("solar", "wind", "gas_cc")[(i + j) % 3],
                    "technologies": {},
                    "capacity_mw": 10.0 + i % 97,
                    "attributes": {},
                    "state_code": ("US-TX", "US-OK", "US-CA")[i % 3],
                    "country": "US",
                    **provenance,
                }
            )
            stages[AssetOwner].append(
                {
                    "id": uuid.uuid4(),
                    "asset_id": asset_id,
                    "organization_id": org_id,
                    "role": "owner",
                    "share_pct": 50.0,
                    "owner_name_raw": f"{name} Company {i}",
                    **provenance,
                }
            )
            stages[Proposal].append(
                {
                    "id": prop_id,
                    "public_id": public_id("prop", prop_id),
                    "slug": f"{prefix}-project-{i}-{j}",
                    "kind": "generation",
                    "name_canonical": f"{name} Project {i}-{j}",
                    "sponsor_org_id": org_id,
                    "technology": "solar",
                    "capacity_mw": 50.0 + j,
                    "jurisdiction": "US-TX",
                    "iso": "ERCOT",
                    "lifecycle_state": ("filed", "withdrawn")[j],
                    "identifiers": {},
                    "field_provenance": {},
                    "overrides": {},
                    "publish_state": "public",
                    "published_at": now - dt.timedelta(days=2),
                    "public_at": now - dt.timedelta(days=1),
                    "min_reuse_class": "open",
                    "source_count": 1,
                }
            )
            stages[ProposalSource].append(
                {
                    "id": uuid.uuid4(),
                    "proposal_id": prop_id,
                    "source_record_id": f"{prefix}{i}-{j}",
                    "raw": {},
                    "normalised": {},
                    **provenance,
                }
            )
    for model, rows in stages.items():
        db.execute(sa.insert(model), rows)
    db.commit()


#: The list requests the budget tests time: a figure sort, a group sort under a state filter, a name
#: search with its count, and a figure filter with its count.
TIMED = (
    {"sort": "-operating_mw"},
    {"sort": "-pipeline_mw", "scope": "group", "state": "US-TX"},
    {"q": "company 11", "include": "count"},
    {"has_assets": "true", "sort": "-assets", "include": "count"},
)


def _assert_within_budget(client: TestClient, label: str) -> None:
    for params in TIMED:
        _get(client, **params)  # warm: imports, the gate key, SQLite's page cache
        median = median_seconds(lambda p=params: _get(client, **p))  # type: ignore[misc]
        assert median < BUDGET_S * MARGIN, f"{params}: median {median * 1000:.0f} ms on {label}"


def test_the_list_answers_within_the_budget_and_the_refresh_is_bounded(
    client: TestClient, db: Session
) -> None:
    """3,000 synthetic companies (a third of the real store's, to keep the suite quick; the real one
    is `test_..._on_a_copy_of_the_full_store`). Measured 2026-10-22 at 12,000 companies: refresh
    9.9 s into an empty table, the four requests 59-99 ms warm."""
    _large_store(db, 3_000)
    started = time.monotonic()
    report = refresh(db)
    db.commit()
    elapsed = time.monotonic() - started
    assert report.rows == 3_000 * 4
    assert elapsed < 30, f"refresh took {elapsed:.1f} s over 3,000 companies"
    _assert_within_budget(client, "3,000 synthetic companies")


def _statements(db: Session, work: Any) -> int:
    count = 0

    def _count(*_args: Any) -> None:
        nonlocal count
        count += 1

    engine = db.get_bind()
    sa.event.listen(engine, "before_cursor_execute", _count)
    try:
        work()
    finally:
        sa.event.remove(engine, "before_cursor_execute", _count)
    return count


def test_the_refresh_costs_the_same_statements_however_many_companies_and_trees(db: Session) -> None:
    _large_store(db, 40)
    small = _statements(db, lambda: org_stats.compute(db))
    _large_store(db, 400, prefix="more")  # ten times the companies, parents and records
    large = _statements(db, lambda: org_stats.compute(db))
    assert large == small, (small, large)


#: A copy of a full store to time against (`ORG_STATS_FULL_STORE=/path/to/store.db`); the lane ran it
#: on the 2026-10-20 build (`web.dev_up --skip-load` serves the same file). Skipped without one.
FULL_STORE = os.environ.get("ORG_STATS_FULL_STORE")


@pytest.mark.skipif(not FULL_STORE, reason="ORG_STATS_FULL_STORE names no store copy")
def test_the_list_answers_within_the_budget_on_a_copy_of_the_full_store(tmp_path: pathlib.Path) -> None:
    from services.api.app import app
    from services.api.deps import get_db
    from services.api.org_stats_refresh import _ensure_table
    from services.db.session import get_engine, get_sessionmaker

    copy = tmp_path / "store.db"
    shutil.copyfile(str(FULL_STORE), copy)
    engine = get_engine(f"sqlite+pysqlite:///{copy}")
    _ensure_table(engine)
    factory = get_sessionmaker(engine)
    with factory() as session:
        report = refresh(session)
        session.commit()
    assert report.rows > 0

    def _override() -> Any:
        with factory() as session:
            yield session
            session.commit()

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as full:
            _assert_within_budget(full, f"a copy of {FULL_STORE}")
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
