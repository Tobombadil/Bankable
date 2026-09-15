"""Contract test (docs/04-standards.md E-8): every implemented public response validates against
its schema in the committed `api/openapi.yaml`, loaded and resolved directly — not re-typed by
hand into a parallel Pydantic model tree that could drift from the committed file.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app
from services.api.conftest import (
    make_event,
    make_location,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.api.deps import get_db
from services.db.session import get_engine, get_sessionmaker, init_db

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
OPENAPI_PATH = REPO_ROOT / "api" / "openapi.yaml"
UTC = dt.UTC


@pytest.fixture(scope="module")
def spec() -> dict:
    with OPENAPI_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def schema_validator(spec: dict, schema_name: str) -> jsonschema.protocols.Validator:
    schema = spec["components"]["schemas"][schema_name]
    resolver = jsonschema.validators.RefResolver.from_schema(spec)
    validator_cls = jsonschema.validators.validator_for(schema)
    return validator_cls(schema, resolver=resolver)


def assert_valid(spec: dict, schema_name: str, instance: object) -> None:
    validator = schema_validator(spec, schema_name)
    errors = sorted(validator.iter_errors(instance), key=str)
    assert not errors, "\n".join(f"{schema_name}: {e.message} at {list(e.absolute_path)}" for e in errors)


@pytest.fixture()
def db_sessionmaker() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


@pytest.fixture()
def seeded_client(db_sessionmaker: sessionmaker[Session]):
    with db_sessionmaker() as db:
        lic = make_open_licence(db)
        src = make_public_source(db, lic)
        org = make_org(db, "Acme Power LLC")
        loc = make_location(db, src, lic)
        prop = make_visible_proposal(db, src, public_id_suffix="1", sponsor=org, location=loc)
        make_event(db, prop, src)
        make_visible_opportunity(db, src, public_id_suffix="1")
        db.commit()

    def _override():
        s = db_sessionmaker()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_openapi_document_parses_and_has_expected_schemas(spec: dict) -> None:
    assert spec["openapi"].startswith("3.1")
    for name in ("ProposalListResponse", "ProposalDetailResponse", "EventListResponse", "Problem"):
        assert name in spec["components"]["schemas"]


def test_list_proposals_matches_schema(spec: dict, seeded_client) -> None:
    resp = seeded_client.get("/v1/proposals")
    assert resp.status_code == 200
    assert_valid(spec, "ProposalListResponse", resp.json())


def test_proposal_detail_matches_schema(spec: dict, seeded_client) -> None:
    resp = seeded_client.get("/v1/proposals")
    public_id_ = resp.json()["data"][0]["public_id"]
    detail = seeded_client.get(f"/v1/proposals/{public_id_}")
    assert detail.status_code == 200
    assert_valid(spec, "ProposalDetailResponse", detail.json())


def test_proposal_events_matches_schema(spec: dict, seeded_client) -> None:
    resp = seeded_client.get("/v1/proposals")
    public_id_ = resp.json()["data"][0]["public_id"]
    events = seeded_client.get(f"/v1/proposals/{public_id_}/events")
    assert events.status_code == 200
    assert_valid(spec, "EventListResponse", events.json())


def test_proposal_sources_matches_schema(spec: dict, seeded_client) -> None:
    resp = seeded_client.get("/v1/proposals")
    public_id_ = resp.json()["data"][0]["public_id"]
    sources = seeded_client.get(f"/v1/proposals/{public_id_}/sources")
    assert sources.status_code == 200
    assert_valid(spec, "ProposalSourceListResponse", sources.json())


def test_proposals_geo_matches_schema(spec: dict, seeded_client) -> None:
    resp = seeded_client.get("/v1/proposals/geo?bbox=-106.6,25.8,-93.5,36.5&zoom=5")
    assert resp.status_code == 200
    assert_valid(spec, "GeoResponse", resp.json())


def test_list_opportunities_matches_schema(spec: dict, seeded_client) -> None:
    resp = seeded_client.get("/v1/opportunities")
    assert resp.status_code == 200
    assert_valid(spec, "OpportunityListResponse", resp.json())


def test_opportunity_detail_matches_schema(spec: dict, seeded_client) -> None:
    resp = seeded_client.get("/v1/opportunities")
    public_id_ = resp.json()["data"][0]["public_id"]
    detail = seeded_client.get(f"/v1/opportunities/{public_id_}")
    assert detail.status_code == 200
    assert_valid(spec, "OpportunityDetailResponse", detail.json())


def test_list_organizations_matches_schema(spec: dict, seeded_client) -> None:
    resp = seeded_client.get("/v1/organizations")
    assert resp.status_code == 200
    assert_valid(spec, "OrganizationListResponse", resp.json())


def test_organization_detail_matches_schema(spec: dict, seeded_client) -> None:
    resp = seeded_client.get("/v1/organizations")
    public_id_ = resp.json()["data"][0]["public_id"]
    detail = seeded_client.get(f"/v1/organizations/{public_id_}")
    assert detail.status_code == 200
    assert_valid(spec, "OrganizationDetailResponse", detail.json())


def test_list_events_matches_schema(spec: dict, seeded_client) -> None:
    resp = seeded_client.get("/v1/events")
    assert resp.status_code == 200
    assert_valid(spec, "EventListResponse", resp.json())


def test_get_event_matches_schema(spec: dict, seeded_client) -> None:
    resp = seeded_client.get("/v1/events")
    event_id = resp.json()["data"][0]["id"]
    detail = seeded_client.get(f"/v1/events/{event_id}")
    assert detail.status_code == 200
    assert_valid(spec, "EventDetailResponse", detail.json())


def test_list_sources_matches_schema(spec: dict, seeded_client) -> None:
    resp = seeded_client.get("/v1/sources")
    assert resp.status_code == 200
    assert_valid(spec, "SourceListResponse", resp.json())


def test_source_detail_matches_schema(spec: dict, seeded_client) -> None:
    resp = seeded_client.get("/v1/sources")
    source_id = resp.json()["data"][0]["source_id"]
    detail = seeded_client.get(f"/v1/sources/{source_id}")
    assert detail.status_code == 200
    assert_valid(spec, "SourceDetailResponse", detail.json())


def test_list_licences_matches_schema(spec: dict, seeded_client) -> None:
    resp = seeded_client.get("/v1/licences")
    assert resp.status_code == 200
    assert_valid(spec, "LicenceListResponse", resp.json())


def test_licence_detail_matches_schema(spec: dict, seeded_client) -> None:
    resp = seeded_client.get("/v1/licences")
    licence_id = resp.json()["data"][0]["licence_id"]
    detail = seeded_client.get(f"/v1/licences/{licence_id}")
    assert detail.status_code == 200
    assert_valid(spec, "LicenceDetailResponse", detail.json())


def test_vocabularies_matches_schema(spec: dict, seeded_client) -> None:
    resp = seeded_client.get("/v1/meta/vocabularies")
    assert resp.status_code == 200
    assert_valid(spec, "VocabulariesResponse", resp.json())


def test_health_matches_schema(spec: dict, seeded_client) -> None:
    resp = seeded_client.get("/v1/health")
    assert resp.status_code == 200
    assert_valid(spec, "HealthResponse", resp.json())


def test_not_found_matches_problem_schema(spec: dict, seeded_client) -> None:
    resp = seeded_client.get("/v1/proposals/prop_00000000ZZ")
    assert resp.status_code == 404
    assert_valid(spec, "Problem", resp.json())


def test_unknown_parameter_matches_problem_schema(spec: dict, seeded_client) -> None:
    resp = seeded_client.get("/v1/proposals?technolgy=x")
    assert resp.status_code == 400
    assert_valid(spec, "Problem", resp.json())


# --------------------------------------------------------------- Pro tier and alerts (this sprint)
# Extends E-8 to the operations `services/api/pro.py` newly ships (`api/openapi.yaml`'s
# `x-status: live` flip for these operationIds is this sprint's own change, so the contract test
# must cover them the same way the pre-existing operations above are covered). Unlike
# `seeded_client` above, this fixture's `_override` commits after each request — `services/api/pro.py`
# is this codebase's first write surface, and `services/api/deps.py::get_db`'s own docstring
# explains why a plain `finally: s.close()` would silently discard those writes.
@pytest.fixture()
def pro_client(db_sessionmaker: sessionmaker[Session]):
    def _override():
        s = db_sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _pro_account_and_user(db_sessionmaker: sessionmaker[Session], client, *, entitlement: str = "pro"):
    from tests.conftest import login, make_account, make_user

    with db_sessionmaker() as db:
        account = make_account(db, entitlement=entitlement)
        user = make_user(db, account)
        db.commit()
        login(client, db, user)
    return account, user


def test_me_matches_schema(spec: dict, db_sessionmaker: sessionmaker[Session], pro_client) -> None:
    _pro_account_and_user(db_sessionmaker, pro_client)
    resp = pro_client.get("/v1/me")
    assert resp.status_code == 200
    assert_valid(spec, "MeResponse", resp.json())


def test_saved_search_create_and_list_match_schema(
    spec: dict, db_sessionmaker: sessionmaker[Session], pro_client
) -> None:
    _pro_account_and_user(db_sessionmaker, pro_client)
    created = pro_client.post(
        "/v1/saved-searches",
        json={"name": "contract test", "entity": "proposal", "query": {"kind": "storage"}},
    )
    assert created.status_code == 201
    assert_valid(spec, "SavedSearchDetailResponse", created.json())

    listing = pro_client.get("/v1/saved-searches")
    assert listing.status_code == 200
    assert_valid(spec, "SavedSearchListResponse", listing.json())


def test_api_key_create_and_list_match_schema(
    spec: dict, db_sessionmaker: sessionmaker[Session], pro_client
) -> None:
    from services.api.pro import API_LICENCE_VERSION

    _pro_account_and_user(db_sessionmaker, pro_client, entitlement="api")
    created = pro_client.post(
        "/v1/keys", json={"name": "contract-key", "licence_accepted_version": API_LICENCE_VERSION}
    )
    assert created.status_code == 201
    assert_valid(spec, "ApiKeyCreatedResponse", created.json())

    listing = pro_client.get("/v1/keys")
    assert listing.status_code == 200
    assert_valid(spec, "ApiKeyListResponse", listing.json())


def test_webhook_create_and_list_match_schema(
    spec: dict, db_sessionmaker: sessionmaker[Session], pro_client
) -> None:
    _pro_account_and_user(db_sessionmaker, pro_client, entitlement="api")
    created = pro_client.post(
        "/v1/webhooks", json={"url": "https://example.com/hook", "types": ["event.published"]}
    )
    assert created.status_code == 201
    assert_valid(spec, "WebhookEndpointCreatedResponse", created.json())

    listing = pro_client.get("/v1/webhooks")
    assert listing.status_code == 200
    assert_valid(spec, "WebhookEndpointListResponse", listing.json())
