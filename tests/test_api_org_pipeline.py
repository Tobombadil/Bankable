"""`GET /v1/organizations/{id}/pipeline` and the list filters its links use (lane P, 2026-10-10).

The contract held here: **every count the aggregate returns is the total of the list query it
documents**, checked against the real API on a fixture company with a group (two subsidiaries, a
grandchild, a hidden subsidiary), at each ownership scope, for every bucket: lifecycle state
(listed and not), the active pipeline and its "no longer listed" twin, technology, grid operator
(with an EIA balancing-authority code beside the ISO's token) and register. Then the same through
the company page's own links (`web/org_pipeline.py::pipeline_summary`) and the list page's own
parameter building (`web/app.py`), so the page's counts equal what each link opens. Also: the new
`sponsor_scope` and `listed` filters, the `listed`/`delisted_at` fields, the `iso` aliases, the
gated cases, the contract schema and the rate-limit window the endpoint spends.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.datastructures import QueryParams

from services.api.conftest import make_open_licence, make_org, make_public_source, make_visible_proposal
from services.db.models import Licence, Organization, Proposal, ProposalSource, Source

UTC = dt.UTC
SPEC = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] / "api" / "openapi.yaml").read_text())
ACTIVE = ("announced", "filed", "studied", "permitted", "contracted", "under_construction")
ALL_STATES = (*ACTIVE, "built", "withdrawn", "cancelled", "unknown")
GONE_EARLY = dt.datetime(2026, 8, 1, tzinfo=UTC)
GONE_LATE = dt.datetime(2026, 9, 15, tzinfo=UTC)


def _validate(schema_name: str, instance: Any) -> None:
    schema = SPEC["components"]["schemas"][schema_name]
    resolver = jsonschema.validators.RefResolver.from_schema(SPEC)
    validator = jsonschema.validators.validator_for(schema)(schema, resolver=resolver)
    errors = sorted(validator.iter_errors(instance), key=str)
    assert not errors, "\n".join(f"{e.message} at {list(e.absolute_path)}" for e in errors)


def _link(
    db: Session, prop: Proposal, source: Source, record_id: str, *, gone: dt.datetime | None = None
) -> None:
    now = dt.datetime.now(UTC)
    db.add(
        ProposalSource(
            proposal_id=prop.id,
            source_id=source.id,
            source_record_id=record_id,
            source_url=f"{source.url}#{record_id}",
            retrieved_at=now - dt.timedelta(hours=2),
            licence_id=source.licence_id,
            first_seen=now - dt.timedelta(days=60),
            last_seen=now - dt.timedelta(days=2),
            gone_at=gone,
        )
    )
    prop.source_count += 1
    db.flush()
    db.expire(prop, ["sources"])


class Store:
    """The fixture company (`root`) and everything around it, by name."""

    def __init__(self, db: Session) -> None:
        self.db = db
        lic = make_open_licence(db)
        self.queue = make_public_source(db, lic, id_="us.test.queue")
        self.inventory = make_public_source(db, lic, id_="us.test.inventory")
        self.root = make_org(db, "Group Holdings")
        self.solar = make_org(db, "Group Solar")
        self.storage = make_org(db, "Group Storage")
        self.grand = make_org(db, "Group Grandchild")
        self.hidden = make_org(db, "Group Hidden Sub")
        self.other = make_org(db, "Unrelated Power")
        self.solar.parent_org_id = self.storage.parent_org_id = self.hidden.parent_org_id = self.root.id
        self.grand.parent_org_id = self.solar.id
        self.hidden.publish_state = "unpublished"
        db.flush()
        self.n = 0
        self.props: dict[str, Proposal] = {}
        add = self.add
        add("root_announced", self.root, "announced", "solar", "ERCOT", 100.0)
        add("solar_filed_ba_code", self.solar, "filed", "solar", "ERCO", 200.0)  # EIA's code for ERCOT
        add("solar_load", self.solar, "filed", "load", "ERCOT", 1000.0, kind="load")
        add("solar_studied", self.solar, "studied", "bess_li_ion", "MISO", 50.5, kind="storage")
        add("storage_contracted", self.storage, "contracted", "solar", "MISO", None)
        add("grand_built", self.grand, "built", "wind", "ERCOT", 300.0)
        add("grand_withdrawn_gone", self.grand, "withdrawn", "solar", "ERCOT", 80.0, gone=[GONE_EARLY])
        add("storage_filed_gone", self.storage, "filed", "solar", "ERCOT", 40.0, gone=[GONE_EARLY, GONE_LATE])
        add("storage_filed_unstated", self.storage, "filed", None, None, 20.0)
        add("storage_half_gone", self.storage, "filed", "solar", "SWPP", 60.0, gone=[GONE_EARLY, None])
        add("hidden_sub", self.hidden, "filed", "solar", "ERCOT", 999.0)
        add("unrelated", self.other, "filed", "solar", "ERCOT", 5.0)
        future = add("root_not_yet_public", self.root, "filed", "solar", "ERCOT", 7.0)
        future.public_at = dt.datetime.now(UTC) + dt.timedelta(days=3)
        db.commit()

    def add(
        self,
        name: str,
        sponsor: Organization,
        state: str,
        technology: str | None,
        iso: str | None,
        mw: float | None,
        *,
        kind: str = "generation",
        gone: list[dt.datetime | None] | None = None,
    ) -> Proposal:
        self.n += 1
        prop = make_visible_proposal(
            self.db, self.queue, public_id_suffix=str(self.n), sponsor=sponsor, lifecycle_state=state
        )
        prop.kind, prop.technology, prop.iso, prop.capacity_mw = kind, technology, iso, mw
        if gone:
            prop.sources[0].gone_at = gone[0]
            for i, when in enumerate(gone[1:], start=1):
                _link(self.db, prop, self.inventory, f"I{self.n}-{i}", gone=when)
        self.db.flush()
        self.props[name] = prop
        return prop


@pytest.fixture()
def store(db: Session) -> Store:
    return Store(db)


def _total(client: TestClient, query: dict[str, str]) -> int:
    resp = client.get("/v1/proposals", params={**query, "include": "count", "limit": 1})
    assert resp.status_code == 200, resp.text
    return int(resp.json()["meta"]["total"])


def _pipeline(client: TestClient, org: Organization, scope: str | None = None) -> dict[str, Any]:
    resp = client.get(f"/v1/organizations/{org.public_id}/pipeline", params={"scope": scope} if scope else {})
    assert resp.status_code == 200, resp.text
    _validate("OrganizationPipelineResponse", resp.json())
    data: dict[str, Any] = resp.json()["data"]
    return data


def _bucket_queries(data: dict[str, Any]) -> Iterator[tuple[str, dict[str, str], int]]:
    """`(label, list query, count)` for every bucket, exactly as the operation documents them."""
    base = dict(data["list_query"])
    active = {"lifecycle_state": ",".join(data["active_states"])}
    yield "totals", {**base, "lifecycle_state": ",".join(ALL_STATES)}, data["totals"]["records"]
    yield "active", {**base, **active, "listed": "true"}, data["active"]["records"]
    yield "not_listed", {**base, **active, "listed": "false"}, data["not_listed"]["records"]
    for entry in data["by_lifecycle_state"]:
        for flag in ("listed", "not_listed"):
            query = {
                **base,
                "lifecycle_state": entry["lifecycle_state"],
                "listed": str(flag == "listed").lower(),
            }
            yield f"{entry['lifecycle_state']}/{flag}", query, entry[flag]["records"]
    for dimension in ("technology", "iso"):
        for entry in data[f"by_{dimension}"]:
            if entry[dimension] is not None:  # a null bucket names no filter
                query = {**base, **active, "listed": "true", dimension: entry[dimension]}
                yield f"{dimension}={entry[dimension]}", query, entry["records"]
    for source in data["sources"]:
        query = {**base, "lifecycle_state": ",".join(ALL_STATES), "source_id": source["source_id"]}
        yield f"source={source['source_id']}", query, source["records"]


# -------------------------------------------------------------------------- count == link
@pytest.mark.parametrize("scope", ["self", "children", "all"])
def test_every_count_is_the_total_of_the_list_query_it_documents(
    client: TestClient, store: Store, scope: str
) -> None:
    data = _pipeline(client, store.root, scope)
    checked = 0
    for label, query, count in _bucket_queries(data):
        assert _total(client, query) == count, (scope, label, query)
        checked += 1
    assert checked >= 8
    # Null buckets are the remainder of their table: no record is counted twice or dropped.
    assert sum(e["records"] for e in data["by_technology"]) == data["active"]["records"]
    assert sum(e["records"] for e in data["by_iso"]) == data["active"]["records"]


def test_the_group_counts_what_the_page_says_it_counts(client: TestClient, store: Store) -> None:
    data = _pipeline(client, store.root, "all")
    assert data["list_query"] == {"sponsor_id": store.root.public_id, "sponsor_scope": "all"}
    assert data["scope"]["organizations"] == 4  # root, two subsidiaries, the grandchild; not the hidden one
    # 10 records: not the hidden subsidiary's, not the unrelated company's, not the one not yet public.
    assert data["totals"] == {
        "records": 10,
        "capacity_mw": 850.5,  # every summed MW: not the load's 1,000, and the contracted one states none
        "capacity_records": 8,
        "not_summed_records": 1,
    }
    assert (
        data["active"]["records"] == 7
    )  # announced, 4 filed (the half-gone one listed), studied, contracted
    assert data["not_listed"] == {
        "records": 1,
        "capacity_mw": 40.0,
        "capacity_records": 1,
        "not_summed_records": 0,
    }
    states = {
        e["lifecycle_state"]: (e["listed"]["records"], e["not_listed"]["records"])
        for e in data["by_lifecycle_state"]
    }
    assert states == {
        "announced": (1, 0),
        "filed": (4, 1),
        "studied": (1, 0),
        "contracted": (1, 0),
        "built": (1, 0),
        "withdrawn": (0, 1),
    }
    assert [e["lifecycle_state"] for e in data["by_lifecycle_state"]][-2:] == ["built", "withdrawn"]
    # One grid, one name: the `ERCO` row is counted under ERCOT, `SWPP` under SPP.
    assert {e["iso"]: e["records"] for e in data["by_iso"]} == {"ERCOT": 3, "MISO": 2, "SPP": 1, None: 1}
    assert [e["iso"] for e in data["by_iso"]][-1] is None
    assert {e["technology"]: e["records"] for e in data["by_technology"]} == {
        "solar": 4,
        "bess_li_ion": 1,
        "load": 1,
        None: 1,
    }
    assert data["not_summed_by_kind"] == [{"kind": "load", "records": 1}]
    sources = {s["source_id"]: (s["records"], s["licence_id"]) for s in data["sources"]}
    assert sources == {"us.test.queue": (10, "open-lic"), "us.test.inventory": (2, "open-lic")}


def test_self_scope_names_no_scope_and_counts_the_company_alone(client: TestClient, store: Store) -> None:
    data = _pipeline(client, store.root)  # `self` is the default, as on the sibling lists
    assert data["scope"]["scope"] == "self" and data["list_query"] == {"sponsor_id": store.root.public_id}
    assert data["totals"]["records"] == 1
    # A company with no subsidiaries at `all` still links without the scope (the same records).
    leaf = _pipeline(client, store.storage, "all")
    assert leaf["list_query"] == {"sponsor_id": store.storage.public_id}


def test_the_company_pages_links_open_lists_whose_totals_are_its_counts(
    client: TestClient, store: Store
) -> None:
    """The 22-of-22 check lane H ran by hand, as a test: every link `web/org_pipeline.py` builds from
    the aggregate, read the way the list page reads its URL (`web/app.py::_proposal_params`, the
    active-states default included), answers the count printed beside it, at each scope."""
    from web.app import _proposal_params, resolve_proposal_lifecycle_param
    from web.org_pipeline import pipeline_summary

    checked: dict[str, int] = {}
    for scope in ("self", "children", "all"):
        summary = pipeline_summary(_pipeline(client, store.root, scope))
        assert summary is not None
        rows = [
            summary["total"],
            summary["active"],
            *summary["by_status"],
            *summary["by_technology"],
            *summary["by_iso"],
        ]
        if summary["not_listed"]["count"]:
            rows.append(summary["not_listed"])
        rows += [{"label": s["name"], "count": s["count"], "href": s["href"]} for s in summary["sources"]]
        checked[scope] = 0
        for row in rows:
            if row["href"] is None:
                continue
            qp = QueryParams(parse_qsl(urlsplit(row["href"]).query))
            params = _proposal_params(qp, lifecycle_csv=resolve_proposal_lifecycle_param(qp)[0])
            query = {k: v for k, v in params.items() if v is not None}
            assert _total(client, query) == row["count"], (scope, row["label"], row["href"])
            checked[scope] += 1
    # Every link the page prints: the company alone sponsors one record (6 links); with its direct
    # subsidiaries, 15; with the grandchild's built and withdrawn records, 17 (total, active, six
    # status rows, the "more no longer listed" link, three technologies, three operators, two
    # registers; the null buckets print no link).
    assert checked == {"self": 6, "children": 15, "all": 17}, checked


# ------------------------------------------------------------------------------ the filters
def test_sponsor_scope_widens_sponsor_id_down_the_tree(client: TestClient, store: Store) -> None:
    root = store.root.public_id
    every = {"lifecycle_state": ",".join(ALL_STATES)}
    assert _total(client, {**every, "sponsor_id": root}) == 1
    assert _total(client, {**every, "sponsor_id": root, "sponsor_scope": "self"}) == 1
    assert _total(client, {**every, "sponsor_id": root, "sponsor_scope": "children"}) == 8
    assert _total(client, {**every, "sponsor_id": root, "sponsor_scope": "all"}) == 10
    # By slug too, and several roots are the union of their groups.
    assert _total(client, {**every, "sponsor_id": store.root.slug, "sponsor_scope": "all"}) == 10
    both = f"{store.solar.public_id},{store.other.slug}"
    assert _total(client, {**every, "sponsor_id": both, "sponsor_scope": "all"}) == 6
    # Nothing to widen without a sponsor: as if absent.
    assert _total(client, {**every, "sponsor_scope": "all"}) == _total(client, every)
    bad = client.get("/v1/proposals", params={"sponsor_id": root, "sponsor_scope": "group"})
    assert bad.status_code == 400 and bad.json()["errors"][0]["field"] == "sponsor_scope"


def test_a_hidden_subsidiary_and_everything_below_it_stay_out_of_the_group(
    client: TestClient, store: Store
) -> None:
    query = {
        "sponsor_id": store.root.public_id,
        "sponsor_scope": "all",
        "lifecycle_state": ",".join(ALL_STATES),
    }
    ids = {r["public_id"] for r in client.get("/v1/proposals", params={**query, "limit": 50}).json()["data"]}
    assert store.props["hidden_sub"].public_id not in ids
    assert store.props["grand_built"].public_id in ids


def test_listed_filters_on_the_registers_the_tier_may_see(client: TestClient, store: Store) -> None:
    every = {
        "sponsor_id": store.root.public_id,
        "sponsor_scope": "all",
        "lifecycle_state": ",".join(ALL_STATES),
    }
    gone = {
        r["public_id"]
        for r in client.get("/v1/proposals", params={**every, "listed": "false"}).json()["data"]
    }
    assert gone == {
        store.props["grand_withdrawn_gone"].public_id,
        store.props["storage_filed_gone"].public_id,
    }
    assert _total(client, {**every, "listed": "true"}) == 8
    bad = client.get("/v1/proposals", params={"listed": "no"})
    assert bad.status_code == 400 and bad.json()["errors"][0]["field"] == "listed"
    assert _total(client, {**every, "listed": ""}) == 10  # an empty value is no filter


def test_the_map_feed_and_export_accept_the_new_filters(client: TestClient, store: Store) -> None:
    query = {"sponsor_id": store.root.public_id, "sponsor_scope": "all", "listed": "false"}
    geo = client.get("/v1/proposals/geo", params={**query, "bbox": "-180,-85,180,85", "zoom": "3"})
    assert geo.status_code == 200, geo.text
    feed = client.get("/feeds/proposals.json", params={**query, "lifecycle_state": ",".join(ALL_STATES)})
    assert feed.status_code == 200, feed.text
    assert len(feed.json()["items"]) == 2


def test_iso_matches_every_spelling_of_the_grid(client: TestClient, store: Store) -> None:
    base = {"sponsor_id": store.solar.public_id, "lifecycle_state": ",".join(ALL_STATES)}
    ercot = {
        r["public_id"] for r in client.get("/v1/proposals", params={**base, "iso": "ERCOT"}).json()["data"]
    }
    assert store.props["solar_filed_ba_code"].public_id in ercot
    assert ercot == {
        r["public_id"] for r in client.get("/v1/proposals", params={**base, "iso": "ERCO"}).json()["data"]
    }
    # Stored values are served as the register gave them.
    detail = client.get(f"/v1/proposals/{store.props['solar_filed_ba_code'].public_id}").json()["data"]
    assert detail["iso"] == "ERCO"


# --------------------------------------------------------------------------------- the fields
def test_listed_and_delisted_at_are_served_on_list_detail_and_bulk_shapes(
    client: TestClient, store: Store
) -> None:
    gone = store.props["storage_filed_gone"].public_id
    half = store.props["storage_half_gone"].public_id
    detail = client.get(f"/v1/proposals/{gone}").json()["data"]
    assert detail["listed"] is False
    assert detail["delisted_at"].startswith("2026-09-15")  # the later of its two links' dates
    assert detail["lifecycle_state"] == "filed"  # the last status stated, not rewritten
    assert client.get(f"/v1/proposals/{half}").json()["data"]["listed"] is True  # one register still lists it
    rows = client.get("/v1/proposals", params={"sponsor_id": store.storage.public_id}).json()["data"]
    by_id = {r["public_id"]: (r["listed"], r["delisted_at"]) for r in rows}
    assert by_id[half] == (True, None)
    assert by_id[gone][0] is False


def test_a_link_the_tier_may_not_see_neither_keeps_a_record_listed_nor_dates_it(
    client: TestClient, db: Session, store: Store
) -> None:
    """The gone record's inventory link (the later date) belongs to a register taken off the public
    surface: the record is judged on the links the tier may see."""
    prop = store.props["storage_filed_gone"]
    _link(db, prop, store.inventory, "I-current")  # a current link, on the same register
    store.inventory.publish_state = "ingest_only"
    db.commit()
    detail = client.get(f"/v1/proposals/{prop.public_id}").json()["data"]
    assert (detail["listed"], detail["delisted_at"][:10]) == (False, "2026-08-01")
    every = {"sponsor_id": store.storage.public_id, "lifecycle_state": ",".join(ALL_STATES)}
    gone = {
        r["public_id"]
        for r in client.get("/v1/proposals", params={**every, "listed": "false"}).json()["data"]
    }
    assert prop.public_id in gone


# ------------------------------------------------------------------------------ gated values
def test_a_counted_field_from_a_hidden_source_is_counted_as_served(
    client: TestClient, db: Session, store: Store
) -> None:
    held = make_public_source(db, db.get(Licence, "open-lic"), id_="us.test.held")
    held.publish_state = "ingest_only"
    prop = store.props["root_announced"]
    prop.sources[0].normalised = {"technology": "solar", "lifecycle_state": "announced"}
    _link(db, prop, held, "H1")
    prop.technology = "geothermal"
    prop.field_provenance = {
        "technology": {"source_id": held.id, "licence_id": "open-lic", "retrieved_at": "x"}
    }
    db.commit()
    data = _pipeline(client, store.root, "self")
    # Stored "geothermal" came from the held register; the served value is the queue's "solar".
    assert [e["technology"] for e in data["by_technology"]] == ["solar"]
    assert data["totals"]["records"] == 1
    assert {s["source_id"] for s in data["sources"]} == {"us.test.queue"}  # the held register is not named


# ------------------------------------------------------------------------------ the operation
def test_unknown_organisation_scope_and_parameters_are_refused(client: TestClient, store: Store) -> None:
    assert client.get("/v1/organizations/org_0000000000/pipeline").status_code == 404
    hidden = client.get(f"/v1/organizations/{store.hidden.public_id}/pipeline")
    assert hidden.status_code == 404  # the same answer a made-up id gets
    bad = client.get(f"/v1/organizations/{store.root.public_id}/pipeline", params={"scope": "group"})
    assert bad.status_code == 400
    extra = client.get(f"/v1/organizations/{store.root.public_id}/pipeline", params={"limit": "5"})
    assert extra.status_code == 400 and extra.json()["code"] == "unknown_parameter"


def test_a_company_that_sponsors_nothing_answers_zeros(client: TestClient, store: Store) -> None:
    lonely = make_org(store.db, "Lonely Developer")
    store.db.commit()
    data = _pipeline(client, lonely)
    assert data["totals"] == {
        "records": 0,
        "capacity_mw": None,
        "capacity_records": 0,
        "not_summed_records": 0,
    }
    assert data["by_lifecycle_state"] == [] and data["sources"] == []


def test_the_pipeline_spends_one_unit_of_the_read_window_and_no_search_unit(
    client: TestClient, store: Store
) -> None:
    from services.api.ratelimit import default_limiter

    default_limiter.reset()
    url = f"/v1/organizations/{store.root.public_id}/pipeline"
    first, second = client.get(url), client.get(url)
    # The read window is the one closest to exhaustion (60 an hour against 1,000 a day), so the
    # headers describe it; a search would have named the `search` window.
    assert first.headers["RateLimit-Policy"] == '60;w=3600;policy="public-read"'
    assert int(first.headers["RateLimit-Remaining"]) - int(second.headers["RateLimit-Remaining"]) == 1


# ------------------------------------------------------------------------------ licence ids
@pytest.mark.parametrize(
    "licence_id",
    [
        "us.eia.860m#f142611d41",  # minted by the registry (`SourceEntry.licence_id`)
        "gb.neso.tec_register#0a1b2c3d4e",
        "curated.organization_parents#5f6e7d8c9b",
        "us.eia.860m#f142611d41-noncommercial",  # an operator's reclassification
        "us.eia.860m#f142611d41-noncommercial-2",
        "caiso-tou",  # hand-made
    ],
)
def test_the_spec_admits_every_licence_id_shape_the_store_mints(licence_id: str) -> None:
    _validate("LicenceIdValue", licence_id)


@pytest.mark.parametrize(
    "bad", ["US.EIA#f142611d41", "us.eia.860m#F142611D41", "us.eia.860m#abc", "-x", "a b"]
)
def test_the_spec_still_refuses_what_is_not_a_licence_id(bad: str) -> None:
    with pytest.raises(AssertionError):
        _validate("LicenceIdValue", bad)


def test_proposal_responses_with_a_real_shaped_licence_id_match_the_contract(
    client: TestClient, db: Session
) -> None:
    lic = make_open_licence(db, id_="us.eia.860m#f142611d41")
    src = make_public_source(db, lic, id_="us.eia.860m")
    org = make_org(db, "Minted Licence Co")
    prop = make_visible_proposal(db, src, sponsor=org)
    db.commit()
    listing = client.get("/v1/proposals")
    assert listing.json()["data"][0]["provenance"][0]["licence_id"] == "us.eia.860m#f142611d41"
    # Served on every row though the schema does not require them (an admin draft has no links).
    detail = client.get(f"/v1/proposals/{prop.public_id}").json()["data"]
    for row in (listing.json()["data"][0], detail):
        assert (row["listed"], row["delisted_at"]) == (True, None)
    _validate("ProposalListResponse", listing.json())
    _validate("ProposalDetailResponse", client.get(f"/v1/proposals/{prop.public_id}").json())
    _validate("ProposalSourceListResponse", client.get(f"/v1/proposals/{prop.public_id}/sources").json())
    _validate(
        "OrganizationPipelineResponse", client.get(f"/v1/organizations/{org.public_id}/pipeline").json()
    )


@pytest.mark.parametrize("key, value", [("sponsor_scope", "all"), ("listed", "false")])
def test_a_saved_search_refuses_a_filter_the_alert_matcher_does_not_evaluate(
    db: Session, store: Store, key: str, value: str
) -> None:
    """The list applies `sponsor_scope` and `listed`; `services/alerts/matching.py` does not yet, so
    a stored query naming one is refused rather than saved as an alert that ignores it."""
    from services.api.errors import ProblemError
    from services.api.pro import validate_saved_search_query

    query = {"sponsor_id": store.root.public_id, key: value}
    with pytest.raises(ProblemError) as exc:
        validate_saved_search_query(db, "proposal", query, "/v1/saved-searches")
    assert exc.value.code == "unknown_parameter"
