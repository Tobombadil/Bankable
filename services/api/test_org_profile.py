"""`GET /v1/organizations/{id}` `profile` (lane org-enrich, 2026-10-22): the company facts
`services/ingest/org_enrich.py` stores in `organization.ids["profile"]`, served each with its own
provenance, gated per fact by its source's visibility, attributed in the envelope, and never inside
`ids`. The profile is written here by the module's own `apply_profiles`, so the stored shape the
route reads is the one the monthly job writes."""

from __future__ import annotations

import datetime as dt
import pathlib
from typing import Any

import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.api.conftest import make_open_licence, make_org, make_public_source, make_visible_proposal
from services.api.serialize import ORG_PROFILE_FIELDS, ORG_PROFILE_KEY
from services.db.models import Organization, Source
from services.ingest import org_enrich as oe
from tests.conftest import login, make_account, make_user

SPEC = yaml.safe_load((pathlib.Path(__file__).resolve().parents[2] / "api" / "openapi.yaml").read_text())
NOW = dt.datetime(2026, 10, 22, 4, 0, tzinfo=dt.UTC)
SEC_AT = dt.datetime(2026, 10, 22, 3, 58, tzinfo=dt.UTC)


def _validate(schema_name: str, instance: Any) -> None:
    schema = SPEC["components"]["schemas"][schema_name]
    resolver = jsonschema.validators.RefResolver.from_schema(SPEC)
    validator = jsonschema.validators.validator_for(schema)(schema, resolver=resolver)
    errors = sorted(validator.iter_errors(instance), key=str)
    assert not errors, "\n".join(f"{e.message} at {list(e.absolute_path)}" for e in errors)


def _decision(org: Organization) -> oe.Decision:
    """NRG Energy's facts as the 2026-10-22 recorded fetch decided them
    (`services/ingest/fixtures/org_enrich_recorded.json`)."""
    lic = oe.source_licences()
    wd, sec = lic[oe.WIKIDATA_SOURCE_ID], lic[oe.SEC_TICKERS_SOURCE_ID]
    page = "https://www.wikidata.org/wiki/Q6955139"

    def field(value: Any, source_id: str, url: str, at: dt.datetime, licence: str) -> dict[str, Any]:
        return {
            "value": value,
            "source_id": source_id,
            "source_url": url,
            "retrieved_at": oe.iso(at),
            "licence_id": licence,
        }

    return oe.Decision(
        organization_id=str(org.id),
        public_id=org.public_id,
        name=org.name_canonical,
        status="matched",
        rule="sec_cik",
        confidence=0.99,
        reasons=["sec_name='NRG ENERGY, INC.'", "corroborated: CIK item Q6955139 has the key"],
        qid="Q6955139",
        cik="0001013871",
        wikidata_rule="sec_cik",
        fields={
            "website": field("https://www.nrg.com/", oe.WIKIDATA_SOURCE_ID, f"{page}#P856", NOW, wd),
            "headquarters": field(
                {"city": "Princeton", "country": "US"}, oe.WIKIDATA_SOURCE_ID, f"{page}#P159", NOW, wd
            ),
            "description": field("American energy company", oe.WIKIDATA_SOURCE_ID, page, NOW, wd),
            "ticker": field(
                {"symbol": "NRG", "exchange": "NYSE"},
                oe.SEC_TICKERS_SOURCE_ID,
                "https://www.sec.gov/edgar/browse/?CIK=1013871",
                SEC_AT,
                sec,
            ),
            "lei": field("5E2UPK5SW04M13XY7I38", oe.WIKIDATA_SOURCE_ID, f"{page}#P1278", NOW, wd),
            "wikipedia": field(
                "https://en.wikipedia.org/wiki/NRG_Energy", oe.WIKIDATA_SOURCE_ID, page, NOW, wd
            ),
        },
    )


@pytest.fixture()
def nrg(db: Session) -> Organization:
    org = make_org(db, "NRG Energy, Inc.")
    org.ids = {"eia_utility_id": "13309"}
    # A sponsored proposal makes it a target: the apply removes the profile of any row that is not.
    make_visible_proposal(db, make_public_source(db, make_open_licence(db)), sponsor=org)
    db.commit()
    result = oe.FetchResult(started_at=NOW, finished_at=NOW, decisions=[_decision(org)], report={})
    oe.apply_profiles(db, result, now=NOW)
    db.refresh(org)
    return org


def _get(client: TestClient, org: Organization) -> dict[str, Any]:
    resp = client.get(f"/v1/organizations/{org.public_id}")
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def test_the_detail_serves_each_fact_with_its_provenance(client: TestClient, nrg: Organization) -> None:
    body = _get(client, nrg)
    data = body["data"]
    profile = data["profile"]
    assert [k for k in profile if k != "match"] == list(ORG_PROFILE_FIELDS)
    assert profile["website"]["value"] == "https://www.nrg.com/" == data["website"]
    assert profile["headquarters"]["value"] == {"city": "Princeton", "country": "US"}
    assert profile["ticker"]["value"] == {"symbol": "NRG", "exchange": "NYSE"}
    wd = profile["description"]["provenance"]
    assert wd["source_id"] == oe.WIKIDATA_SOURCE_ID and wd["reuse_class"] == "open"
    assert wd["source_url"] == "https://www.wikidata.org/wiki/Q6955139"
    assert wd["retrieved_at"] == "2026-10-22T04:00:00Z"
    assert wd["attribution_text"] == "Source: Wikidata (CC0)"
    sec = profile["ticker"]["provenance"]
    assert sec["source_id"] == oe.SEC_TICKERS_SOURCE_ID
    assert sec["source_url"] == "https://www.sec.gov/edgar/browse/?CIK=1013871"
    assert sec["retrieved_at"] == "2026-10-22T03:58:00Z"
    assert profile["match"] == {
        "rule": "sec_cik",
        "confidence": 0.99,
        "wikidata_id": "Q6955139",
        "cik": "0001013871",
        "matched_at": "2026-10-22T04:00:00Z",
    }
    _validate("Organization", data)
    _validate("OrganizationProfile", profile)


def test_the_profile_is_never_inside_ids_and_the_match_is_not_an_identifier(
    client: TestClient, nrg: Organization
) -> None:
    """A scored match (0.99) is served as `profile.match`, with its confidence; `ids` stays what
    the registers recorded (the company page's JSON-LD prints `ids` as identifiers)."""
    data = _get(client, nrg)["data"]
    assert ORG_PROFILE_KEY not in data["ids"]
    assert data["ids"] == {"eia_utility_id": "13309"}
    listed = client.get("/v1/organizations").json()["data"]
    row = next(r for r in listed if r["public_id"] == nrg.public_id)
    assert ORG_PROFILE_KEY not in row["ids"] and "profile" not in row


def test_the_envelope_attributes_the_sources_of_the_facts_shown(
    client: TestClient, nrg: Organization
) -> None:
    summary = _get(client, nrg)["licence_summary"]
    by_source = {s["source_id"]: s for s in summary["sources"]}
    assert set(by_source) == {oe.WIKIDATA_SOURCE_ID, oe.SEC_TICKERS_SOURCE_ID}
    assert by_source[oe.WIKIDATA_SOURCE_ID]["record_count"] == 5
    assert by_source[oe.SEC_TICKERS_SOURCE_ID]["record_count"] == 1


def test_a_fact_whose_source_is_not_public_is_null_and_not_attributed(
    client: TestClient, db: Session, nrg: Organization
) -> None:
    src = db.get(Source, oe.WIKIDATA_SOURCE_ID)
    assert src is not None
    src.publish_state = "ingest_only"
    db.commit()
    body = _get(client, nrg)
    profile = body["data"]["profile"]
    # The website the enrichment wrote goes with its source, on every organisation surface.
    assert body["data"]["website"] is None
    listed = client.get("/v1/organizations").json()["data"]
    assert next(r for r in listed if r["public_id"] == nrg.public_id)["website"] is None
    assert {k: v is None for k, v in profile.items() if k != "match"} == {
        "website": True,
        "headquarters": True,
        "description": True,
        "ticker": False,
        "lei": True,
        "wikipedia": True,
    }
    assert [s["source_id"] for s in body["licence_summary"]["sources"]] == [oe.SEC_TICKERS_SOURCE_ID]
    sec = db.get(Source, oe.SEC_TICKERS_SOURCE_ID)
    assert sec is not None
    sec.publish_state = "ingest_only"
    db.commit()
    body = _get(client, nrg)
    assert body["data"]["profile"] is None and body["licence_summary"]["sources"] == []


def test_a_curated_website_hides_the_stored_one(client: TestClient, db: Session, nrg: Organization) -> None:
    nrg.website = "https://example.org/nrg-curated"
    db.commit()
    data = _get(client, nrg)["data"]
    assert data["website"] == "https://example.org/nrg-curated"
    assert data["profile"]["website"] is None and data["profile"]["description"] is not None
    # A curated website is not Wikidata's, so unpublishing Wikidata leaves it.
    src = db.get(Source, oe.WIKIDATA_SOURCE_ID)
    assert src is not None
    src.publish_state = "ingest_only"
    db.commit()
    assert _get(client, nrg)["data"]["website"] == "https://example.org/nrg-curated"


def test_an_admin_edit_that_echoes_ids_keeps_the_profile(
    client: TestClient, db: Session, nrg: Organization
) -> None:
    """The admin view serves `ids` without the profile; a PATCH that sends that `ids` back, with
    an identifier added, keeps it (and the website it wrote), so the next month's apply still knows
    what it wrote. The admin view shows the stored website whatever its source's state."""
    admin = make_user(
        db, make_account(db, entitlement="admin", name="Ops"), email="ops@example.com", role="operator"
    )
    db.commit()
    login(client, db, admin)
    path = f"/admin/v1/organizations/{nrg.public_id}"
    got = client.get(path).json()["data"]
    assert ORG_PROFILE_KEY not in got["ids"]
    resp = client.patch(
        path, json={"ids": {**got["ids"], "duns": "123456789"}, "reason": "DUNS from a filing"}
    )
    assert resp.status_code == 200, resp.text
    db.expire_all()
    org = db.get(Organization, nrg.id)
    assert org is not None and org.ids is not None
    assert org.ids["duns"] == "123456789" and org.ids[ORG_PROFILE_KEY]["website_written"] == org.website
    src = db.get(Source, oe.WIKIDATA_SOURCE_ID)
    assert src is not None
    src.publish_state = "ingest_only"
    db.commit()
    assert client.get(path).json()["data"]["website"] == "https://www.nrg.com/"
    assert _get(client, org)["data"]["website"] is None


def test_a_natural_person_and_an_organisation_with_no_profile_get_null(
    client: TestClient, db: Session, nrg: Organization
) -> None:
    plain = make_org(db, "Plain Developer LLC")
    db.commit()
    assert _get(client, plain)["data"]["profile"] is None
    nrg.personal_data = True
    db.commit()
    assert _get(client, nrg)["data"]["profile"] is None


def test_the_api_serves_the_module_s_profile_key_and_fields() -> None:
    """The route reads what `services/ingest/org_enrich.py` writes; the two constants are one
    contract, held here because services.ingest never imports services.api."""
    assert ORG_PROFILE_KEY == oe.PROFILE_KEY and ORG_PROFILE_FIELDS == oe.PROFILE_FIELDS
