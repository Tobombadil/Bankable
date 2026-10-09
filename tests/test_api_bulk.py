"""NDJSON bulk streams (US-703; docs/23 §3.2, §5 `read:bulk`, §6 "20 bulk requests / hour", §7
limit 1,000): `GET /v1/bulk/proposals`, `/v1/bulk/opportunities`, `/v1/bulk/events`. Every test
reads the body line by line, the way an integrator's client does."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any

import jsonschema
import pytest
import yaml
from starlette.requests import Request

from services.api.bulk import BULK_MAX_LIMIT, bulk_limit
from services.api.conftest import (
    make_attribution_licence,
    make_event,
    make_location,
    make_open_licence,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from tests.conftest import login, make_account, make_api_key, make_user

UTC = dt.UTC
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / "api" / "openapi.yaml").read_text())


def assert_valid(spec: dict[str, Any], schema_name: str, instance: object) -> None:
    schema = spec["components"]["schemas"][schema_name]
    resolver = jsonschema.validators.RefResolver.from_schema(spec)
    validator = jsonschema.validators.validator_for(schema)(schema, resolver=resolver)
    errors = sorted(validator.iter_errors(instance), key=str)
    assert not errors, "\n".join(f"{e.message} at {list(e.absolute_path)}" for e in errors)


def _bulk_key(db, *, entitlement: str = "api", scopes: list[str] | None = None) -> dict[str, str]:
    account = make_account(db, entitlement=entitlement)
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user, scopes=scopes or ["read:live", "read:bulk"])
    db.commit()
    return {"Authorization": f"Bearer {secret}"}


def _lines(resp) -> list[dict[str, Any]]:
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in resp.iter_lines() if line]
    assert lines and lines[0]["record_type"] == "meta"
    return lines


def _seed(db, n: int = 3):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    props = [make_visible_proposal(db, src, public_id_suffix=str(i)) for i in range(1, n + 1)]
    db.commit()
    return lic, src, props


def test_bulk_proposals_streams_a_meta_line_then_the_detail_shape(client, db, spec):
    _lic, _src, props = _seed(db, n=2)
    headers = _bulk_key(db)
    resp = client.get("/v1/bulk/proposals", headers=headers)
    lines = _lines(resp)
    meta, records = lines[0], lines[1:]
    assert_valid(spec, "BulkMetaLine", meta)
    assert meta["page"] == {"next_cursor": None, "prev_cursor": None, "has_more": False}
    assert meta["meta"]["tier"] == "api"
    assert meta["licence_summary"]["sources"][0]["record_count"] == 2
    assert len(records) == 2
    for record in records:
        assert_valid(spec, "Proposal", record)
        assert record["provenance"] and record["provenance"][0]["source_url"]
        detail = client.get(f"/v1/proposals/{record['public_id']}", headers=headers).json()["data"]
        assert record == detail
    # Oldest change first.
    assert [r["public_id"] for r in records] == [p.public_id for p in props]
    assert resp.headers["RateLimit-Policy"] == '20;w=3600;policy="api-bulk"'
    assert resp.headers["cache-control"] == "private, no-store"


def test_bulk_pages_with_a_cursor_and_limit(client, db):
    _seed(db, n=3)
    headers = _bulk_key(db)
    first = _lines(client.get("/v1/bulk/proposals?limit=2", headers=headers))
    assert first[0]["page"]["has_more"] is True
    cursor = first[0]["page"]["next_cursor"]
    second = _lines(client.get("/v1/bulk/proposals", params={"limit": 2, "cursor": cursor}, headers=headers))
    assert second[0]["page"]["has_more"] is False
    seen = [r["public_id"] for r in first[1:] + second[1:]]
    assert len(seen) == 3 and len(set(seen)) == 3
    assert client.get("/v1/bulk/proposals?cursor=@@", headers=headers).status_code == 400


def test_bulk_limit_defaults_to_one_thousand_and_refuses_more():
    from services.api.errors import ProblemError

    def req(qs: str) -> Request:
        return Request(
            {"type": "http", "method": "GET", "path": "/", "query_string": qs.encode(), "headers": []}
        )

    assert bulk_limit(req("")) == BULK_MAX_LIMIT == 1000
    assert bulk_limit(req("limit=7")) == 7
    assert bulk_limit(req("limit=1000")) == 1000
    # Clamped silently until 2026-10-07 (backend audit API-5); the spec's `BulkLimit` is 1..1,000.
    for bad in ("limit=5000", "limit=0"):
        with pytest.raises(ProblemError) as raised:
            bulk_limit(req(bad))
        assert raised.value.code == "validation_error"


def test_updated_since_is_the_incremental_sync_filter(client, db):
    _lic, _src, props = _seed(db, n=2)
    props[0].last_changed = dt.datetime(2026, 1, 1, tzinfo=UTC)
    db.commit()
    headers = _bulk_key(db)
    lines = _lines(client.get("/v1/bulk/proposals?updated_since=2026-06-01T00:00:00Z", headers=headers))
    assert [r["public_id"] for r in lines[1:]] == [props[1].public_id]
    bad = client.get("/v1/bulk/proposals?updated_since=yesterday", headers=headers)
    assert bad.status_code == 400
    assert bad.json()["code"] == "validation_error"


def test_bulk_filters_and_unknown_parameters(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_visible_proposal(db, src, public_id_suffix="1", jurisdiction="US-TX")
    make_visible_proposal(db, src, public_id_suffix="2", jurisdiction="US-CA")
    db.commit()
    headers = _bulk_key(db)
    lines = _lines(client.get("/v1/bulk/proposals?jurisdiction=US-CA", headers=headers))
    assert [r["jurisdiction"] for r in lines[1:]] == ["US-CA"]
    # The list endpoint's `technology` is not a documented bulk parameter.
    resp = client.get("/v1/bulk/proposals?technology=solar_pv", headers=headers)
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_parameter"


@pytest.mark.parametrize(
    ("setup", "status", "code"),
    [
        ("anonymous", 401, "unauthenticated"),
        ("session", 403, "forbidden_tier"),
        ("read_live_key", 403, "forbidden_tier"),
        ("bulk_key_on_pro_plan", 403, "forbidden_tier"),
    ],
)
def test_bulk_needs_an_api_plan_key_with_read_bulk(client, db, setup, status, code):
    _seed(db, n=1)
    headers: dict[str, str] = {}
    if setup == "session":
        account = make_account(db, entitlement="api")
        user = make_user(db, account)
        db.commit()
        login(client, db, user)
    elif setup == "read_live_key":
        headers = _bulk_key(db, scopes=["read:live"])
    elif setup == "bulk_key_on_pro_plan":
        headers = _bulk_key(db, entitlement="pro")
    resp = client.get("/v1/bulk/proposals", headers=headers)
    assert resp.status_code == status
    assert resp.json()["code"] == code


def test_bulk_bucket_is_twenty_an_hour(client, db):
    _seed(db, n=1)
    headers = _bulk_key(db)
    for i in range(20):
        resp = client.get("/v1/bulk/events", headers=headers)
        assert resp.status_code == 200, (i, resp.text)
    assert resp.headers["RateLimit-Remaining"] == "0"
    over = client.get("/v1/bulk/proposals", headers=headers)
    assert over.status_code == 429
    assert over.json()["code"] == "rate_limited"
    assert int(over.headers["Retry-After"]) >= 0
    assert over.headers["RateLimit-Policy"].endswith('policy="api-bulk"')
    # The read bucket is separate: a plain read still answers.
    assert client.get("/v1/proposals", headers=headers).status_code == 200


def test_licence_shape_absent_without_api_redistribution_and_derived_only_without_bulk_export(client, db):
    no_api = make_attribution_licence(db, id_="no-api")
    no_api.allows_api_redistribution = False
    make_visible_proposal(db, make_public_source(db, no_api, id_="us.test.no_api"), public_id_suffix="1")
    no_bulk = make_attribution_licence(db, id_="no-bulk")
    no_bulk.allows_bulk_export = False
    kept = make_visible_proposal(
        db, make_public_source(db, no_bulk, id_="us.test.no_bulk"), public_id_suffix="2"
    )
    db.commit()
    headers = _bulk_key(db)
    lines = _lines(client.get("/v1/bulk/proposals", headers=headers))
    assert [r["public_id"] for r in lines[1:]] == [kept.public_id]
    assert lines[1]["provenance"][0]["source_record_id"] is None
    assert lines[1]["provenance"][0]["source_id"] == "us.test.no_bulk"
    assert lines[0]["redactions"] == [
        {
            "public_id": kept.public_id,
            "field": "provenance[0].source_record_id",
            "reason": "licence",
            "source_id": "us.test.no_bulk",
            "note": "the source licence does not permit bulk export; derived fields only",
        }
    ]
    assert [s["source_id"] for s in lines[0]["licence_summary"]["sources"]] == ["us.test.no_bulk"]


def test_a_location_whose_licence_forbids_api_redistribution_does_not_travel(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    loc_lic = make_attribution_licence(db, id_="loc-no-api")
    loc_lic.allows_api_redistribution = False
    loc_src = make_public_source(db, loc_lic, id_="us.test.loc_source")
    loc = make_location(db, loc_src, loc_lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1", location=loc)
    db.commit()
    lines = _lines(client.get("/v1/bulk/proposals", headers=_bulk_key(db)))
    assert lines[1]["location"] is None
    assert_valid(spec, "Proposal", lines[1])
    assert lines[0]["redactions"][0]["field"] == "location"
    assert lines[0]["redactions"][0]["public_id"] == prop.public_id


def test_bulk_opportunities_include_every_status_unless_filtered(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_visible_opportunity(db, src, public_id_suffix="1", status="open")
    make_visible_opportunity(db, src, public_id_suffix="2", status="closed")
    db.commit()
    headers = _bulk_key(db)
    lines = _lines(client.get("/v1/bulk/opportunities", headers=headers))
    assert sorted(r["status"] for r in lines[1:]) == ["closed", "open"]
    for record in lines[1:]:
        assert_valid(spec, "Opportunity", record)
    only_open = _lines(client.get("/v1/bulk/opportunities?status=open", headers=headers))
    assert [r["status"] for r in only_open[1:]] == ["open"]


def test_bulk_events_are_ordered_by_seq_and_resume_from_since(client, db, spec):
    _lic, src, props = _seed(db, n=1)
    events = [make_event(db, props[0], src, event_type=t) for t in ("created", "status_change", "filed")]
    db.commit()
    headers = _bulk_key(db)
    lines = _lines(client.get("/v1/bulk/events", headers=headers))
    assert [r["seq"] for r in lines[1:]] == sorted(e.seq for e in events)
    for record in lines[1:]:
        assert_valid(spec, "Event", record)
        assert record["provenance"]["source_id"] == src.id
    assert_valid(spec, "BulkMetaLine", lines[0])
    resumed = _lines(client.get(f"/v1/bulk/events?since={events[0].seq}", headers=headers))
    assert [r["seq"] for r in resumed[1:]] == sorted(e.seq for e in events[1:])
    assert client.get("/v1/bulk/events?since=not-a-time", headers=headers).status_code == 400


def test_a_bulk_proposal_page_costs_a_fixed_number_of_queries(client, db, db_sessionmaker):
    """Backend audit 2026-10-07 PERF-1: the `members[]` block read every link's deferred
    `normalised` column, one lazy load per link (1,013 queries for a 1,000-row page on the audit
    store). The page's query count must not grow with its size."""
    from sqlalchemy import event as sa_event

    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    for i in range(1, 3):
        make_visible_proposal(db, src, public_id_suffix=str(i))
    db.commit()
    headers = _bulk_key(db)
    statements: list[str] = []

    def count(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    engine = db_sessionmaker.kw["bind"]
    sa_event.listen(engine, "before_cursor_execute", count)
    try:
        small = _lines(client.get("/v1/bulk/proposals", headers=headers))
        few = len(statements)
        for i in range(10, 30):
            make_visible_proposal(db, src, public_id_suffix=str(i))
        db.commit()
        statements.clear()
        large = _lines(client.get("/v1/bulk/proposals", headers=headers))
        many = len(statements)
    finally:
        sa_event.remove(engine, "before_cursor_execute", count)
    assert len(small) == 3 and len(large) == 23
    assert all(line["members"] for line in large[1:])
    assert many == few, (few, many)
