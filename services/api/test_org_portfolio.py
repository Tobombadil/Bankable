"""`GET /v1/organizations/{id}/portfolio` (2026-10-21, lane W1; docs/16 §5).

The contract held here: **every count the aggregate returns is the number of distinct assets its
list query returns** from `GET /v1/organizations/{id}/assets`, checked against the real list on a
fixture group (two subsidiaries, a grandchild, a taken-down subsidiary, an unrelated company), at
each scope, for every bucket. Then the rules the numbers rest on, one test each: an asset two
companies in the scope hold counts once; owned MW takes the largest register's summed shares, never
the sum across registers, and caps at 100; retired assets are reported beside the headline and not
in it; restricted licences, taken-down organisations and edges only a hidden register states lend
nothing; assets with no MW are counted and not summed; scope self against all. Also the registers,
a natural person's share, the posture, parameter errors, the documented schema and the cost.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import uuid
from collections.abc import Iterator
from typing import Any

import jsonschema
import pytest
import sqlalchemy as sa
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.api.conftest import (
    make_asset,
    make_asset_owner,
    make_open_licence,
    make_org,
    make_public_source,
)
from services.api.ratelimit import default_limiter
from services.api.visibility import PUBLISHABLE_REUSE_CLASSES
from services.db.models import Asset, AssetOwner, Licence, Organization, Source
from services.ids import public_id
from tests.latency import median_seconds

UTC = dt.UTC
SPEC = yaml.safe_load((pathlib.Path(__file__).resolve().parents[2] / "api" / "openapi.yaml").read_text())
BUCKETS = ("by_status", "by_role", "by_technology", "by_state", "by_asset_type")
#: Names that must never reach a public response: what a restricted licence, a hidden register or a
#: taken-down organisation holds.
GATED_NAMES = (
    "Restricted Licence Plant",
    "Gated Edge Plant",
    "Hidden Sub Plant",
    "Fleet Hidden Sub",
    "us.test.restricted_owners",
    "us.test.staged_owners",
)


def _validate(schema_name: str, instance: Any) -> None:
    schema = SPEC["components"]["schemas"][schema_name]
    resolver = jsonschema.validators.RefResolver.from_schema(SPEC)
    validator = jsonschema.validators.validator_for(schema)(schema, resolver=resolver)
    errors = sorted(validator.iter_errors(instance), key=str)
    assert not errors, "\n".join(f"{e.message} at {list(e.absolute_path)}" for e in errors)


def _licence(db: Session, id_: str, reuse_class: str) -> Licence:
    open_ = reuse_class != "restricted"
    lic = Licence(
        id=id_,
        name=f"{reuse_class.title()} Licence",
        reuse_class=reuse_class,
        allows_derived_publication=open_,
        allows_raw_publication=open_,
        allows_api_redistribution=open_,
        allows_bulk_export=open_,
        allows_commercial_use=reuse_class != "noncommercial" and open_,
        gate_flag=not open_,
        evidence_url=f"https://example.org/{id_}",
        evidence_retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        classified_by="legal-compliance",
    )
    db.add(lic)
    db.flush()
    return lic


def _source(db: Session, lic: Licence, id_: str, name: str, publish_state: str = "public") -> Source:
    src = make_public_source(db, lic, id_=id_)
    src.name, src.url, src.publish_state = name, f"https://example.org/{id_}", publish_state
    db.flush()
    return src


class Store:
    """The fixture group (`root`) and everything around it, by name."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.open = make_open_licence(db)
        self.restricted = _licence(db, "restricted-lic", "restricted")
        self.plants = _source(db, self.open, "us.test.plants", "Test Plant Register")
        self.sub_shares = _source(db, self.open, "us.test.owner_shares", "Test Owner Shares")
        self.parent_shares = _source(db, self.open, "us.test.emitters", "Test Emitter Register")
        self.operators = _source(db, self.open, "us.test.operators", "Test Operator Register")
        self.restricted_owners = _source(
            db, self.restricted, "us.test.restricted_owners", "Restricted Owners"
        )
        self.staged_owners = _source(db, self.open, "us.test.staged_owners", "Staged Owners", "ingest_only")

        self.root = make_org(db, "Fleet Holdings")
        self.gen_a = make_org(db, "Fleet Generation A")
        self.gen_b = make_org(db, "Fleet Generation B")
        self.grand = make_org(db, "Fleet Wind Grandchild")
        self.hidden = make_org(db, "Fleet Hidden Sub")
        self.other = make_org(db, "Unrelated Utility")
        self.gen_a.parent_org_id = self.gen_b.parent_org_id = self.hidden.parent_org_id = self.root.id
        self.grand.parent_org_id = self.gen_a.id
        self.hidden.publish_state = "unpublished"
        db.flush()

        self.n = 0
        self.assets: dict[str, Asset] = {}
        add, hold = self.add, self.hold
        # Two subsidiaries hold 40% and 30% in one register; the parent's own register says 60%; a
        # restricted register says the parent holds all of it; an unrelated company holds 30%.
        shared = add("Shared Gas Plant", "gas_cc", 1000.0, "US-TX")
        hold(shared, self.gen_a, self.sub_shares, share=40.0)
        hold(shared, self.gen_b, self.sub_shares, share=30.0)
        hold(shared, self.root, self.parent_shares, share=60.0)
        hold(shared, self.root, self.restricted_owners, share=100.0)
        hold(shared, self.other, self.sub_shares, share=30.0)
        # One register's shares add up to 130%.
        over = add("Over Solar Farm", "solar", 200.0, "US-CA")
        hold(over, self.gen_a, self.sub_shares, share=80.0)
        hold(over, self.grand, self.sub_shares, share=50.0)
        hold(add("Operated Wind Farm", "wind", 500.0, "US-TX"), self.gen_b, self.operators, role="operator")
        hold(
            add("Retired Coal Plant", "coal", 300.0, "US-OH", status="retired"),
            self.gen_a,
            self.sub_shares,
            100.0,
        )
        storage = add(
            "Standby Gas Storage", "depleted_field", None, "US-TX", status="standby", type_="gas_storage"
        )
        hold(storage, self.gen_b, self.operators, role="operator")
        hold(add("Retiring Peaker", "gas_ct", 150.0, "US-OK", status="retiring"), self.gen_a, self.sub_shares)
        hold(add("Unstated Plant", None, 50.0, None), self.grand, self.operators, role="operator")
        # What must lend nothing.
        restricted = add("Restricted Licence Plant", "gas_cc", 900.0, "US-TX", source=self.restricted_owners)
        hold(restricted, self.gen_a, self.sub_shares, share=100.0)
        gated = add("Gated Edge Plant", "gas_cc", 400.0, "US-TX")
        hold(gated, self.gen_b, self.restricted_owners, share=100.0)
        hold(gated, self.gen_a, self.staged_owners, share=100.0)
        hold(add("Hidden Sub Plant", "gas_cc", 700.0, "US-TX"), self.hidden, self.sub_shares, share=100.0)
        hold(add("Unrelated Plant", "gas_cc", 800.0, "US-TX"), self.other, self.sub_shares, share=100.0)
        db.commit()

    def add(
        self,
        name: str,
        technology: str | None,
        mw: float | None,
        state: str | None,
        *,
        status: str = "operating",
        type_: str = "power_plant",
        source: Source | None = None,
    ) -> Asset:
        self.n += 1
        src = source or self.plants
        asset = make_asset(
            self.db,
            src,
            self.db.get(Licence, src.licence_id),  # type: ignore[arg-type]
            source_asset_id=str(self.n),
            asset_type=type_,
            name=name,
            status=status,
            technology=technology,
            capacity_mw=mw,
            state_code=state,
        )
        self.assets[name] = asset
        return asset

    def hold(
        self,
        asset: Asset,
        org: Organization,
        source: Source,
        share: float | None = None,
        *,
        role: str = "owner",
        retrieved: dt.datetime | None = None,
    ) -> None:
        edge = make_asset_owner(
            self.db, asset, org, source, self.db.get(Licence, source.licence_id), role=role, share_pct=share
        )
        if retrieved is not None:
            edge.retrieved_at = retrieved
        self.db.flush()


@pytest.fixture()
def store(db: Session) -> Store:
    return Store(db)


def _portfolio(client: TestClient, org: Organization, **params: str) -> dict[str, Any]:
    default_limiter.reset()
    resp = client.get(f"/v1/organizations/{org.public_id}/portfolio", params=params)
    assert resp.status_code == 200, resp.text
    _validate("OrganizationAssetPortfolioResponse", resp.json())
    data: dict[str, Any] = resp.json()["data"]
    return data


def _listed(client: TestClient, org: Organization, query: dict[str, str]) -> set[str]:
    """The distinct assets `GET /v1/organizations/{id}/assets` returns for `query`, every page."""
    seen: set[str] = set()
    cursor = None
    while True:
        default_limiter.reset()
        params = {**query, "limit": "200", **({"cursor": cursor} if cursor else {})}
        resp = client.get(f"/v1/organizations/{org.public_id}/assets", params=params)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        seen.update(row["name"] for row in body["data"])
        cursor = body["page"]["next_cursor"]
        if not cursor:
            return seen


def _buckets(data: dict[str, Any]) -> Iterator[tuple[str, dict[str, str], int]]:
    """`(label, list query, count)` for the headline and every bucket that names a query."""
    yield "totals", data["list_query"], data["totals"]["assets"]
    for key in BUCKETS:
        for entry in data[key]:
            if entry["list_query"] is not None:
                yield f"{key}={entry['list_query']}", entry["list_query"], entry["assets"]


def _bucket(data: dict[str, Any], key: str, token: str | None) -> dict[str, Any]:
    field = {"by_status": "status", "by_role": "role", "by_technology": "technology"}.get(key)
    field = field or {"by_state": "state_code", "by_asset_type": "asset_type"}[key]
    entry: dict[str, Any] = next(e for e in data[key] if e[field] == token)
    return entry


# -------------------------------------------------------------------------- count == list total
@pytest.mark.parametrize(
    ("scope", "status"),
    [("self", None), ("children", None), ("all", None), ("all", "retired"), ("all", "operating,retired")],
)
def test_every_count_is_the_distinct_assets_of_the_list_query_it_names(
    client: TestClient, store: Store, scope: str, status: str | None
) -> None:
    data = _portfolio(client, store.root, scope=scope, **({"status": status} if status else {}))
    checked = 0
    for label, query, count in _buckets(data):
        assert len(_listed(client, store.root, query)) == count, (scope, label, query)
        checked += 1
    assert checked >= 3
    # Null buckets are the remainder of their table: no asset is counted twice or dropped.
    for key in ("by_technology", "by_state", "by_asset_type"):
        assert sum(e["assets"] for e in data[key]) == data["totals"]["assets"], key


def test_the_group_counts_what_the_page_says_it_counts(client: TestClient, store: Store) -> None:
    data = _portfolio(client, store.root, scope="all")
    assert data["scope"]["organizations"] == 4  # root, two subsidiaries, the grandchild; not the hidden one
    assert data["statuses"] == ["operating", "standby", "retiring"]
    assert data["list_query"] == {"scope": "all", "status": "operating,standby,retiring"}
    # Six assets today: shared 1,000 + solar 200 + wind 500 + peaker 150 + unstated 50 MW, and the
    # storage field, which states no MW. Owned: 70% of the shared plant and all of the solar farm.
    assert data["totals"] == {
        "assets": 6,
        "capacity_mw": 1900.0,
        "capacity_assets": 5,
        "owned_mw": 900.0,
        "owned_mw_assets": 2,
    }
    assert [(e["status"], e["assets"], e["capacity_mw"]) for e in data["by_status"]] == [
        ("operating", 4, 1750.0),
        ("standby", 1, None),
        ("retiring", 1, 150.0),
        ("retired", 1, 300.0),
    ]
    assert [(e["role"], e["assets"]) for e in data["by_role"]] == [("owner", 3), ("operator", 3)]
    assert [(e["technology"], e["assets"], e["capacity_mw"]) for e in data["by_technology"]] == [
        ("gas_cc", 1, 1000.0),
        ("wind", 1, 500.0),
        ("solar", 1, 200.0),
        ("gas_ct", 1, 150.0),
        ("depleted_field", 1, None),
        (None, 1, 50.0),
    ]
    assert [(e["state_code"], e["assets"], e["capacity_mw"]) for e in data["by_state"]] == [
        ("US-TX", 3, 1500.0),
        ("US-CA", 1, 200.0),
        ("US-OK", 1, 150.0),
        (None, 1, 50.0),
    ]
    assert _bucket(data, "by_technology", None)["list_query"] is None
    assert _bucket(data, "by_state", "US-TX")["list_query"] == {
        "scope": "all",
        "status": "operating,standby,retiring",
        "state": "US-TX",
    }
    assert [(e["asset_type"], e["assets"]) for e in data["by_asset_type"]] == [
        ("power_plant", 5),
        ("gas_storage", 1),
    ]


# -------------------------------------------------------------------------- the rules
def test_an_asset_held_by_two_companies_in_the_scope_counts_once(client: TestClient, store: Store) -> None:
    data = _portfolio(client, store.root, scope="children", status="operating")
    # The shared plant has three in-scope owner edges (both subsidiaries, and the parent through
    # its own register); it is one asset, once under `owner`.
    assert _bucket(data, "by_technology", "gas_cc")["assets"] == 1
    assert _bucket(data, "by_role", "owner")["assets"] == 2  # the shared plant and the solar farm
    rows = client.get(
        f"/v1/organizations/{store.root.public_id}/assets",
        params={"scope": "children", "technology": "gas_cc"},
    ).json()["data"]
    assert sorted(r["held_by"]["name_canonical"] for r in rows) == [
        "Fleet Generation A",
        "Fleet Generation B",
        "Fleet Holdings",
    ]
    assert {r["name"] for r in rows} == {"Shared Gas Plant"}


def test_owned_mw_takes_the_largest_register_never_the_sum_and_caps_at_100(
    client: TestClient, store: Store
) -> None:
    group = _portfolio(client, store.root, scope="all")
    # Shared plant: the subsidiaries' register says 40 + 30 = 70%, the parent's says 60%; the larger
    # is the group's stake, not 130% and not 100%. The restricted register's 100% lends nothing.
    assert _bucket(group, "by_technology", "gas_cc")["owned_mw"] == 700.0
    # Solar farm: one register's shares add up to 80 + 50 = 130%, capped at the whole plant.
    assert _bucket(group, "by_technology", "solar")["owned_mw"] == 200.0
    # An operator edge states no share; nor does an owner edge whose register gives none.
    assert _bucket(group, "by_technology", "wind")["owned_mw"] is None
    assert _bucket(group, "by_technology", "gas_ct") == {
        "technology": "gas_ct",
        "assets": 1,
        "capacity_mw": 150.0,
        "capacity_assets": 1,
        "owned_mw": None,
        "owned_mw_assets": 0,
        "list_query": {"scope": "all", "status": "operating,standby,retiring", "technology": "gas_ct"},
    }
    # Only the shares the scope's own holders have count: without the grandchild, the solar farm is
    # 80% owned; the parent alone holds 60% of the shared plant through its register.
    assert (
        _bucket(_portfolio(client, store.root, scope="children"), "by_technology", "solar")["owned_mw"]
        == 160.0
    )
    assert _bucket(_portfolio(client, store.root), "by_technology", "gas_cc")["owned_mw"] == 600.0


def test_retired_is_reported_beside_the_headline_and_counted_only_when_asked(
    client: TestClient, store: Store
) -> None:
    default = _portfolio(client, store.root, scope="all")
    assert "coal" not in {e["technology"] for e in default["by_technology"]}
    assert _bucket(default, "by_status", "retired") == {
        "status": "retired",
        "assets": 1,
        "capacity_mw": 300.0,
        "capacity_assets": 1,
        "owned_mw": 300.0,
        "owned_mw_assets": 1,
        "list_query": {"scope": "all", "status": "retired"},
    }
    retired = _portfolio(client, store.root, scope="all", status="retired")
    assert retired["statuses"] == ["retired"]
    assert retired["totals"]["assets"] == 1 and retired["totals"]["capacity_mw"] == 300.0
    assert retired["by_status"] == default["by_status"]  # every status, whatever `status=` says
    both = _portfolio(client, store.root, scope="all", status="retired,operating")
    assert both["statuses"] == ["operating", "retired"]  # vocabulary order
    assert both["totals"]["assets"] == 5 and both["list_query"]["status"] == "operating,retired"


def test_restricted_licences_taken_down_orgs_and_gated_edges_lend_nothing(
    client: TestClient, store: Store
) -> None:
    default_limiter.reset()
    resp = client.get(f"/v1/organizations/{store.root.public_id}/portfolio", params={"scope": "all"})
    data = resp.json()["data"]
    for name in GATED_NAMES:
        assert name not in resp.text, name
    assert (
        data["totals"]["assets"] == 6
    )  # not the restricted asset, the gated edge's plant or the hidden sub's
    registers = {r["source_id"] for r in [*data["asset_registers"], *data["ownership_registers"]]}
    assert registers == {"us.test.plants", "us.test.owner_shares", "us.test.emitters", "us.test.operators"}
    assert {s["source_id"] for s in resp.json()["licence_summary"]["sources"]} == registers
    # The list the counts name agrees: none of those rows is listed either.
    listed = _listed(client, store.root, {"scope": "all"})
    assert not listed & {
        "Restricted Licence Plant",
        "Gated Edge Plant",
        "Hidden Sub Plant",
        "Unrelated Plant",
    }
    # Generation B's 100% of the gated plant is stated only by the restricted register: it holds the
    # shared plant (its own 30%) and operates the wind farm, and nothing else it may show.
    gen_b = _portfolio(client, store.gen_b, status="operating")
    assert [(e["role"], e["assets"]) for e in gen_b["by_role"]] == [("owner", 1), ("operator", 1)]
    assert _bucket(gen_b, "by_technology", "gas_cc")["owned_mw"] == 300.0
    # A taken-down organisation is a 404, as an unknown one is.
    assert client.get(f"/v1/organizations/{store.hidden.public_id}/portfolio").status_code == 404


def test_assets_without_mw_are_counted_not_summed(client: TestClient, store: Store) -> None:
    data = _portfolio(client, store.root, scope="all")
    assert _bucket(data, "by_asset_type", "gas_storage") == {
        "asset_type": "gas_storage",
        "assets": 1,
        "capacity_mw": None,  # null, never 0
        "capacity_assets": 0,
        "owned_mw": None,
        "owned_mw_assets": 0,
        "list_query": {"scope": "all", "status": "operating,standby,retiring", "asset_type": "gas_storage"},
    }
    assert data["totals"]["assets"] - data["totals"]["capacity_assets"] == 1


def test_scope_self_counts_the_company_and_all_counts_the_group(client: TestClient, store: Store) -> None:
    own = _portfolio(client, store.root)
    assert own["scope"]["scope"] == "self" and own["scope"]["organizations"] == 1
    assert own["list_query"] == {"status": "operating,standby,retiring"}  # no `scope`: the list's default
    assert own["totals"] == {
        "assets": 1,
        "capacity_mw": 1000.0,
        "capacity_assets": 1,
        "owned_mw": 600.0,
        "owned_mw_assets": 1,
    }
    children = _portfolio(client, store.root, scope="children")
    assert (
        children["scope"]["organizations"] == 3 and children["totals"]["assets"] == 5
    )  # not the grandchild's
    group = _portfolio(client, store.root, scope="all")
    assert group["totals"]["assets"] == 6
    # A company with no subsidiary names no scope, whatever was asked: the rows are the same.
    leaf = _portfolio(client, store.grand, scope="all")
    assert leaf["list_query"] == {"status": "operating,standby,retiring"}
    assert leaf["totals"]["assets"] == 2


def test_the_registers_and_their_latest_retrieval(client: TestClient, store: Store, db: Session) -> None:
    late = dt.datetime(2026, 10, 9, 22, 27, tzinfo=UTC)
    store.assets["Operated Wind Farm"].retrieved_at = dt.datetime(2026, 10, 9, 19, 51, tzinfo=UTC)
    store.hold(
        store.assets["Operated Wind Farm"], store.gen_a, store.parent_shares, share=10.0, retrieved=late
    )
    db.commit()
    data = _portfolio(client, store.root, scope="all")
    assert data["asset_registers"] == [
        {
            "source_id": "us.test.plants",
            "name": "Test Plant Register",
            "licence_id": "open-lic",
            "reuse_class": "open",
            "attribution_text": None,
            "assets": 7,  # every status: the retired plant too
            "retrieved_at_max": "2026-10-09T19:51:00Z",
        }
    ]
    assert [(r["source_id"], r["assets"], r["retrieved_at_max"]) for r in data["ownership_registers"]] == [
        ("us.test.owner_shares", 4, "2026-09-01T00:00:00Z"),
        ("us.test.operators", 3, "2026-09-01T00:00:00Z"),
        ("us.test.emitters", 2, "2026-10-09T22:27:00Z"),
    ]
    summary = {
        s["source_id"]: s
        for s in client.get(
            f"/v1/organizations/{store.root.public_id}/portfolio", params={"scope": "all"}
        ).json()["licence_summary"]["sources"]
    }
    assert summary["us.test.plants"]["record_count"] == 7
    assert summary["us.test.emitters"]["retrieved_at_max"] == "2026-10-09T22:27:00Z"


def test_a_natural_persons_share_is_not_used(client: TestClient, db: Session) -> None:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    person = make_org(db, "Jane Q Landowner")
    person.personal_data = True
    plant = make_asset(db, src, lic, source_asset_id="p1", name="Family Wind", capacity_mw=10.0)
    make_asset_owner(db, plant, person, src, lic, share_pct=50.0)
    db.commit()
    data = _portfolio(client, person)
    # Withheld on every row (`personal_share`), so not multiplied into a total that would reveal it.
    assert data["totals"] == {
        "assets": 1,
        "capacity_mw": 10.0,
        "capacity_assets": 1,
        "owned_mw": None,
        "owned_mw_assets": 0,
    }


def test_a_noncommercial_edge_counts_only_under_the_posture_that_publishes_it(
    client: TestClient, db: Session
) -> None:
    lic = make_open_licence(db)
    nc = _licence(db, "nc-lic", "noncommercial")
    plants = make_public_source(db, lic)
    owners = _source(db, nc, "us.test.nc_owners", "Noncommercial Owners")
    org = make_org(db, "Posture Power")
    plant = make_asset(db, plants, lic, source_asset_id="nc1", name="Posture Plant")
    make_asset_owner(db, plant, org, owners, nc, share_pct=100.0)
    db.commit()
    published = "noncommercial" in PUBLISHABLE_REUSE_CLASSES
    assert _portfolio(client, org)["totals"]["assets"] == (1 if published else 0)
    assert len(_listed(client, org, {})) == (1 if published else 0)


def test_an_organisation_with_no_assets_reads_as_empty(client: TestClient, db: Session) -> None:
    org = make_org(db, "Paper Developer")
    db.commit()
    default_limiter.reset()
    resp = client.get(f"/v1/organizations/{org.public_id}/portfolio")
    data = resp.json()["data"]
    assert data["totals"] == {
        "assets": 0,
        "capacity_mw": None,
        "capacity_assets": 0,
        "owned_mw": None,
        "owned_mw_assets": 0,
    }
    assert all(data[key] == [] for key in (*BUCKETS, "asset_registers", "ownership_registers"))
    assert resp.json()["licence_summary"]["sources"] == []


@pytest.mark.parametrize(
    ("params", "status", "code", "field"),
    [
        ({"status": "planned"}, 400, "validation_error", "status"),
        ({"scope": "everything"}, 400, "validation_error", "scope"),
        ({"include_subsidiaries": "true"}, 400, "unknown_parameter", "include_subsidiaries"),
        ({"technology": "gas_cc"}, 400, "unknown_parameter", "technology"),
    ],
)
def test_parameters_are_checked(
    client: TestClient, store: Store, params: dict[str, str], status: int, code: str, field: str
) -> None:
    resp = client.get(f"/v1/organizations/{store.root.public_id}/portfolio", params=params)
    assert resp.status_code == status, resp.text
    assert resp.json()["code"] == code and resp.json()["errors"][0]["field"] == field


def test_an_unknown_organisation_is_a_404_as_a_taken_down_one_is(client: TestClient, store: Store) -> None:
    assert client.get(f"/v1/organizations/{store.root.public_id}/portfolio").status_code == 200
    unknown = client.get("/v1/organizations/org_doesnotexist/portfolio")
    hidden = client.get(f"/v1/organizations/{store.hidden.public_id}/portfolio")
    assert unknown.status_code == hidden.status_code == 404
    assert unknown.json()["code"] == hidden.json()["code"] == "not_found"


# -------------------------------------------------------------------------- cost
def _group(db: Session, assets: int, *, prefix: str) -> Organization:
    """A parent over ten subsidiaries holding `assets` plants between them, each with an owner edge
    in two registers and an operator edge. Inserted in one flush, so a large group builds quickly."""
    key = prefix.lower()
    lic = make_open_licence(db, id_=f"{key}-lic")
    plants = make_public_source(db, lic, id_=f"us.test.{key}.plants")
    shares = make_public_source(db, lic, id_=f"us.test.{key}.shares")
    parent = make_org(db, f"{prefix} Parent")
    subs = [make_org(db, f"{prefix} Sub {i}") for i in range(10)]
    for sub in subs:
        sub.parent_org_id = parent.id
    retrieved = dt.datetime(2026, 9, 1, tzinfo=UTC)
    rows: list[Any] = []
    for i in range(assets):
        asset_id = uuid.uuid4()
        rows.append(
            Asset(
                id=asset_id,
                public_id=public_id("asset", asset_id),
                slug=f"{key}-plant-{i}",
                asset_type="power_plant",
                source_asset_id=f"{key}{i}",
                name=f"{prefix} Plant {i}",
                status="operating",
                technology=("gas_cc", "wind", "solar")[i % 3],
                technologies={},
                capacity_mw=10.0 + i,
                attributes={},
                state_code=("US-TX", "US-OK")[i % 2],
                country="US",
                source_id=plants.id,
                source_url=plants.url,
                retrieved_at=retrieved,
                licence_id=lic.id,
            )
        )
        holders = (
            (subs[i % 10], plants, "owner", 50.0),
            (parent, shares, "owner", 40.0),
            (subs[(i + 1) % 10], plants, "operator", None),
        )
        rows.extend(
            AssetOwner(
                asset_id=asset_id,
                organization_id=holder.id,
                role=role,
                share_pct=share,
                owner_name_raw=holder.name_canonical,
                source_id=src.id,
                source_url=src.url,
                retrieved_at=retrieved,
                licence_id=lic.id,
            )
            for holder, src, role, share in holders
        )
    db.add_all(rows)
    db.commit()
    return parent


def _statements(client: TestClient, db: Session, org: Organization) -> int:
    count = 0

    def _count(*_args: Any) -> None:
        nonlocal count
        count += 1

    engine = db.get_bind()
    sa.event.listen(engine, "before_cursor_execute", _count)
    try:
        _portfolio(client, org, scope="all")
    finally:
        sa.event.remove(engine, "before_cursor_execute", _count)
    return count


def test_the_statement_count_does_not_grow_with_the_fleet(client: TestClient, db: Session) -> None:
    small = _statements(client, db, _group(db, 10, prefix="Small"))
    large = _statements(client, db, _group(db, 200, prefix="Large"))
    assert large == small, (small, large)


#: docs/04 E-16's list budget (300 ms p95), with the margin the coverage budget test allows an
#: unknown runner. Measured 2026-10-21: 19-35 ms warm on the 2026-10-09 store copy (NextEra, Duke
#: and Invenergy, at most 49 assets a group); on this synthetic group 55 ms at 300 assets, 99 ms at
#: 1,000 and 262 ms at 3,000 (three edges each, median of 7, uninstrumented).
BUDGET_S = 0.3
MARGIN = 3.0


def test_a_large_group_portfolio_answers_within_the_list_budget(client: TestClient, db: Session) -> None:
    parent = _group(db, 1000, prefix="Budget")  # 20 times the largest group in the store
    _portfolio(client, parent, scope="all")  # first call: imports and caches
    median = median_seconds(lambda: _portfolio(client, parent, scope="all"))
    assert median < BUDGET_S * MARGIN, f"portfolio median {median * 1000:.0f} ms over 1,000 assets"
