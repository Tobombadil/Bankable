"""CSV exports (US-603 AC1-AC3, US-105 AC2; docs/23 §1 `Accept: text/csv`, §3.2, §6, §10):
`POST /v1/exports`, `GET /v1/exports`, `GET /v1/exports/{id}`, the owner-only download route, and
the list endpoints' `Accept: text/csv` twin. Fixtures only; every file lands in a per-test
`EXPORT_DIR`."""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import pathlib
from typing import Any

import jsonschema
import pytest
import yaml

from services.api import exports as exports_module
from services.api import ratelimit
from services.api.conftest import (
    make_attribution_licence,
    make_event,
    make_open_licence,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.api.exports import (
    DERIVED_COLUMNS,
    PROVENANCE_COLUMNS,
    _event_row,
    _opportunity_row,
    _proposal_row,
    export_path,
)
from services.api.ratelimit import PlanQuota
from services.api.resource_queries import subject_info, subject_infos
from services.db.models import Export, Licence
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


@pytest.fixture(autouse=True)
def _export_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    path = tmp_path / "exports"
    monkeypatch.setenv("EXPORT_DIR", str(path))
    return path


def _login(client, db, *, entitlement: str = "pro", email: str = "ana@example.com"):
    account = make_account(db, entitlement=entitlement, name=f"Account {email}")
    user = make_user(db, account, email=email)
    db.commit()
    login(client, db, user)
    return account, user


def _seed(db, n: int = 3):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    props = [make_visible_proposal(db, src, public_id_suffix=str(i)) for i in range(1, n + 1)]
    db.commit()
    return lic, src, props


def _read_csv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    header = [line for line in text.splitlines() if line.startswith("#")]
    body = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    return header, list(csv.DictReader(io.StringIO(body)))


def _create(client, **body: Any):
    payload = {"entity": "proposal", "query": {}, **body}
    return client.post("/v1/exports", json=payload)


# ------------------------------------------------------------------------------ POST and download
def test_create_export_writes_a_csv_with_the_licence_block_and_provenance_on_every_row(client, db, spec):
    _seed(db)
    _login(client, db)
    resp = _create(client)
    assert resp.status_code == 202, resp.text
    assert_valid(spec, "ExportDetailResponse", resp.json())
    data = resp.json()["data"]
    assert data["status"] == "ready"
    assert data["row_count"] == 3
    assert data["row_cap"] == 10_000
    assert data["truncated"] is False
    assert data["download_url"].endswith(f"/v1/exports/{data['export_id']}/download")
    assert resp.headers["RateLimit-Policy"].endswith('policy="pro-read"')

    download = client.get(f"/v1/exports/{data['export_id']}/download")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("text/csv")
    assert download.headers["cache-control"] == "private, no-store"
    text = download.text
    assert text.startswith("# Infraque CSV export ")
    assert "· licences=open-lic ·" in text.splitlines()[0]
    header, rows = _read_csv(text)
    assert any(line.startswith("# Sources: Test Public Source") for line in header)
    assert any(line.startswith("# us.test.public_source: Open Licence (open)") for line in header)
    assert any("terms=" in line for line in header)
    assert len(rows) == 3
    assert list(rows[0].keys()) == [*DERIVED_COLUMNS["proposal"], *PROVENANCE_COLUMNS]
    for row in rows:
        for column in ("source_id", "source_url", "retrieved_at", "licence"):
            assert row[column], (column, row)
        assert row["licence"] == "open-lic"
        assert row["source_record_id"].startswith("Q")


def test_export_respects_filters_and_sort(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_visible_proposal(db, src, public_id_suffix="1", jurisdiction="US-TX")
    make_visible_proposal(db, src, public_id_suffix="2", jurisdiction="US-CA")
    make_visible_proposal(db, src, public_id_suffix="3", jurisdiction="US-CA")
    db.commit()
    _login(client, db)
    data = _create(client, query={"jurisdiction": ["US-CA"], "sort": "capacity_mw"}).json()["data"]
    assert data["row_count"] == 2
    assert data["query"] == {"jurisdiction": ["US-CA"], "sort": "capacity_mw"}
    _header, rows = _read_csv(client.get(f"/v1/exports/{data['export_id']}/download").text)
    assert [r["jurisdiction"] for r in rows] == ["US-CA", "US-CA"]
    assert [float(r["capacity_mw"]) for r in rows] == [102.0, 103.0]


def test_export_respects_tier_visibility_and_the_bulk_export_licence_flag(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    now = dt.datetime.now(UTC)
    # Not yet public, already published: the Pro tier reads `published_at` (visibility.py).
    make_visible_proposal(db, src, public_id_suffix="1", public_at=now + dt.timedelta(days=3))
    hidden_src = make_public_source(db, lic, id_="us.test.ingest_only")
    hidden_src.publish_state = "ingest_only"
    make_visible_proposal(db, hidden_src, public_id_suffix="2")
    no_bulk = make_attribution_licence(db, id_="no-bulk")
    no_bulk.allows_bulk_export = False
    no_bulk_src = make_public_source(db, no_bulk, id_="us.test.no_bulk")
    make_visible_proposal(db, no_bulk_src, public_id_suffix="3")
    db.commit()
    _login(client, db)
    data = _create(client).json()["data"]
    _header, rows = _read_csv(client.get(f"/v1/exports/{data['export_id']}/download").text)
    assert [r["name_canonical"] for r in rows] == ["Test Storage Project 1"]
    assert data["row_count"] == 1


def test_row_cap_truncates_and_says_so(client, db, monkeypatch):
    _seed(db, n=3)
    monkeypatch.setitem(
        ratelimit.PLAN_QUOTAS,
        "pro",
        PlanQuota(exports_per_day=5, export_rows_max=2, bulk_requests_per_hour=None),
    )
    _login(client, db)
    data = _create(client).json()["data"]
    assert (data["row_cap"], data["row_count"], data["truncated"]) == (2, 2, True)
    text = client.get(f"/v1/exports/{data['export_id']}/download").text
    assert "row_cap=2 · truncated=true" in text.splitlines()[0]
    assert len(_read_csv(text)[1]) == 2


def test_daily_quota_is_enforced_per_user_and_failed_attempts_do_not_count(client, db, monkeypatch):
    _seed(db, n=1)
    monkeypatch.setitem(
        ratelimit.PLAN_QUOTAS,
        "pro",
        PlanQuota(exports_per_day=2, export_rows_max=10, bulk_requests_per_hour=None),
    )
    _login(client, db)
    # A generation failure (not a bad request: that is a 400 before any row exists, below).
    real_write = exports_module._write_csv
    calls = {"n": 0}

    def _fail_once(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk full")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(exports_module, "_write_csv", _fail_once)
    failed = _create(client).json()["data"]
    assert failed["status"] == "failed"
    assert failed["error"] and failed["download_url"] is None
    assert _create(client).status_code == 202
    assert _create(client).status_code == 202
    over = _create(client)
    assert over.status_code == 429
    assert over.json()["code"] == "quota_exceeded"
    assert 0 < int(over.headers["Retry-After"]) <= 86_400
    # Every attempt is logged, the refused one excepted (it never became an export).
    assert db.query(Export).count() == 3


def test_export_needs_a_pro_or_api_credential(client, db):
    assert _create(client).status_code == 401
    _login(client, db, entitlement="public")
    resp = _create(client)
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden_tier"


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"entity": "match"}, "entity"),
        ({"query": None}, "query"),
        ({"query": ["kind"]}, "query"),
        ({"query": {"limit": 5}}, "query.limit"),
        ({"query": {"kind": {"a": 1}}}, "query.kind"),
        ({"query": {"kind": [{"a": 1}]}}, "query.kind"),
        ({"columns": ["public_id", "nope"]}, "columns"),
        ({"columns": "public_id"}, "columns"),
    ],
)
def test_export_request_validation(client, db, body, field):
    _login(client, db)
    resp = _create(client, **body)
    assert resp.status_code == 400, resp.text
    assert field in resp.text


def test_unknown_filter_is_rejected_like_the_list_endpoint(client, db):
    _login(client, db)
    resp = _create(client, query={"colour": "red"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_parameter"


def test_columns_narrow_the_derived_set_but_never_the_provenance(client, db):
    _seed(db, n=1)
    _login(client, db)
    data = _create(client, columns=["capacity_mw", "public_id"]).json()["data"]
    _header, rows = _read_csv(client.get(f"/v1/exports/{data['export_id']}/download").text)
    assert list(rows[0].keys()) == ["public_id", "capacity_mw", *PROVENANCE_COLUMNS]


def test_export_from_a_saved_search(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_visible_proposal(db, src, public_id_suffix="1", jurisdiction="US-TX")
    make_visible_proposal(db, src, public_id_suffix="2", jurisdiction="US-CA")
    db.commit()
    _login(client, db)
    search = client.post(
        "/v1/saved-searches", json={"name": "CA", "entity": "proposal", "query": {"jurisdiction": "US-CA"}}
    ).json()["data"]
    data = client.post(
        "/v1/exports", json={"entity": "proposal", "saved_search_id": search["saved_search_id"]}
    ).json()["data"]
    assert data["row_count"] == 1
    assert data["query"] == {"jurisdiction": "US-CA"}
    assert (
        client.post(
            "/v1/exports", json={"entity": "event", "saved_search_id": search["saved_search_id"]}
        ).status_code
        == 400
    )
    missing = client.post("/v1/exports", json={"entity": "proposal", "saved_search_id": "ss_0000000000"})
    assert missing.status_code == 404


def test_opportunity_and_event_exports(client, db):
    lic, src, props = _seed(db, n=1)
    make_visible_opportunity(db, src, public_id_suffix="1")
    make_event(db, props[0], src)
    db.commit()
    _login(client, db)
    opp = _create(client, entity="opportunities").json()["data"]
    assert opp["entity"] == "opportunity" and opp["row_count"] == 1
    _header, rows = _read_csv(client.get(f"/v1/exports/{opp['export_id']}/download").text)
    assert rows[0]["title"] == "Test RFP 1" and rows[0]["technologies"] == "solar_pv"
    assert rows[0]["source_record_id"] == "N1"
    evt = _create(client, entity="event", query={"subject_type": "proposal"}).json()["data"]
    assert evt["row_count"] == 1
    _header, rows = _read_csv(client.get(f"/v1/exports/{evt['export_id']}/download").text)
    assert rows[0]["subject_id"] == props[0].public_id
    assert rows[0]["event_type"] == "status_change"
    assert rows[0]["after"] == '{"lifecycle_state": "filed"}'
    assert rows[0]["source_id"] == src.id and rows[0]["licence"] == lic.id


# ------------------------------------------------------------------------------ list, get, expiry
def test_list_and_get_are_per_user_and_the_download_is_owner_only(client, db, spec):
    _seed(db, n=1)
    _login(client, db)
    mine = _create(client).json()["data"]
    listing = client.get("/v1/exports")
    assert listing.status_code == 200
    assert_valid(spec, "ExportListResponse", listing.json())
    assert [e["export_id"] for e in listing.json()["data"]] == [mine["export_id"]]
    got = client.get(f"/v1/exports/{mine['export_id']}")
    assert got.status_code == 200
    assert_valid(spec, "ExportDetailResponse", got.json())
    assert client.get("/v1/exports?colour=red").status_code == 400

    client.cookies.clear()
    _login(client, db, email="other@example.com")
    assert client.get("/v1/exports").json()["data"] == []
    assert client.get(f"/v1/exports/{mine['export_id']}").status_code == 404
    assert client.get(f"/v1/exports/{mine['export_id']}/download").status_code == 404


def test_expired_export_reads_expired_and_its_file_is_gone(client, db, _export_dir):
    _seed(db, n=1)
    _login(client, db)
    data = _create(client).json()["data"]
    row = db.query(Export).one()
    db.refresh(row)
    row.expires_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    db.commit()
    assert (_export_dir / f"{data['export_id']}.csv").exists()
    got = client.get(f"/v1/exports/{data['export_id']}").json()["data"]
    assert got["status"] == "expired" and got["download_url"] is None
    assert not (_export_dir / f"{data['export_id']}.csv").exists()
    assert client.get(f"/v1/exports/{data['export_id']}/download").status_code == 404


def test_download_404s_when_the_file_has_gone_missing(client, db, _export_dir):
    _seed(db, n=1)
    _login(client, db)
    data = _create(client).json()["data"]
    (_export_dir / f"{data['export_id']}.csv").unlink()
    assert client.get(f"/v1/exports/{data['export_id']}/download").status_code == 404


def test_api_key_caller_is_logged_against_the_key_and_its_creator(client, db, caplog):
    _seed(db, n=1)
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    key, secret = make_api_key(db, account, user, scopes=["read:live", "read:bulk"])
    db.commit()
    with caplog.at_level(logging.INFO, logger="services.api.exports"):
        resp = client.post(
            "/v1/exports",
            json={"entity": "proposal", "query": {}},
            headers={"Authorization": f"Bearer {secret}"},
        )
    assert resp.status_code == 202
    row = db.query(Export).one()
    db.refresh(row)
    assert (row.user_id, row.api_key_id, row.tier, row.row_count) == (user.id, key.id, "api", 1)
    assert any(
        "export public_id=" in r.getMessage() and "status=ready" in r.getMessage() for r in caplog.records
    )


# ------------------------------------------------------------------------------ Accept: text/csv
@pytest.mark.parametrize(
    ("path", "columns"),
    [("/v1/proposals", "proposal"), ("/v1/opportunities", "opportunity"), ("/v1/events", "event")],
)
def test_accept_text_csv_on_the_list_endpoints(client, db, path, columns):
    _lic, src, props = _seed(db, n=1)
    make_visible_opportunity(db, src, public_id_suffix="1")
    make_event(db, props[0], src)
    db.commit()
    _login(client, db)
    resp = client.get(path, headers={"Accept": "text/csv"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["X-Export-Id"].startswith("exp_")
    header, rows = _read_csv(resp.text)
    assert header and len(rows) == 1
    assert list(rows[0].keys()) == [*DERIVED_COLUMNS[columns], *PROVENANCE_COLUMNS]  # type: ignore[index]
    # Counted and logged like any export (US-603 AC3).
    assert db.query(Export).count() == 1
    # A browser's HTML-first Accept is not a CSV request.
    assert (
        client.get(path, headers={"Accept": "text/html,text/csv"})
        .headers["content-type"]
        .startswith("application/json")
    )


def test_accept_text_csv_page_parameters_are_dropped_and_filters_kept(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    for i, j in ((1, "US-TX"), (2, "US-CA"), (3, "US-CA")):
        make_visible_proposal(db, src, public_id_suffix=str(i), jurisdiction=j)
    db.commit()
    _login(client, db)
    resp = client.get("/v1/proposals?jurisdiction=US-CA&limit=1", headers={"Accept": "text/csv"})
    assert len(_read_csv(resp.text)[1]) == 2
    row = db.query(Export).one()
    assert row.query == {"jurisdiction": "US-CA"}
    assert client.get("/v1/proposals?colour=red", headers={"Accept": "text/csv"}).status_code == 400


def test_accept_text_csv_needs_pro(client, db):
    _seed(db, n=1)
    assert client.get("/v1/proposals", headers={"Accept": "text/csv"}).status_code == 401
    _login(client, db, entitlement="public")
    assert client.get("/v1/proposals", headers={"Accept": "text/csv"}).status_code == 403


def test_accept_text_csv_failure_is_a_503_and_still_logged(client, db, monkeypatch):
    _seed(db, n=1)
    _login(client, db)

    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("disk full")

    monkeypatch.setattr(exports_module, "_write_csv", _fail)
    resp = client.get("/v1/proposals", headers={"Accept": "text/csv"})
    assert resp.status_code == 503
    rows = db.query(Export).all()
    assert [r.status for r in rows] == ["failed"]


def test_accept_text_csv_bad_value_is_the_lists_400_and_writes_no_row(client, db):
    """A value the JSON list refuses is the caller's error on the CSV twin too: the list's own
    `400 validation_error`, not a `503`, and no export row (before 2026-09-27 it was a 503 and a
    `failed` row)."""
    _seed(db, n=1)
    _login(client, db)
    json_resp = client.get("/v1/proposals?capacity_mw[gte]=abc")
    csv_resp = client.get("/v1/proposals?capacity_mw[gte]=abc", headers={"Accept": "text/csv"})
    assert json_resp.status_code == csv_resp.status_code == 400
    assert csv_resp.json()["code"] == json_resp.json()["code"] == "validation_error"
    assert db.query(Export).count() == 0


@pytest.mark.parametrize(
    "query",
    [
        {"capacity_mw[gte]": "not-a-number"},
        {"sort": "not_a_sort_field"},
        {"placement": "nowhere"},
    ],
)
def test_post_exports_bad_value_is_a_400_before_any_row(client, db, query):
    _seed(db, n=1)
    _login(client, db)
    resp = _create(client, query=query)
    assert resp.status_code == 400
    assert resp.json()["code"] == "validation_error"
    assert db.query(Export).count() == 0


# ------------------------------------------------------------------------------ units
def test_derived_columns_are_exactly_the_row_builders_keys(db):
    _lic, src, props = _seed(db, n=1)
    opp = make_visible_opportunity(db, src, public_id_suffix="9")
    ev = make_event(db, props[0], src)
    db.commit()
    assert tuple(_proposal_row(props[0])) == DERIVED_COLUMNS["proposal"]
    assert tuple(_opportunity_row(opp)) == DERIVED_COLUMNS["opportunity"]
    assert tuple(_event_row(ev, subject_infos(db, [ev])[ev.subject_id])) == DERIVED_COLUMNS["event"]


def test_export_path_refuses_keys_outside_the_directory():
    with pytest.raises(ValueError, match="escapes"):
        export_path("../outside.csv")


def test_plan_quota_table():
    assert ratelimit.plan_quota("pro").exports_per_day == 5
    assert ratelimit.plan_quota("pro").export_rows_max == 10_000
    assert ratelimit.plan_quota("api").bulk_requests_per_hour == 20
    assert ratelimit.plan_quota("public") == ratelimit.NO_QUOTA
    assert ratelimit.plan_quota("free_account").exports_per_day is None


def test_licence_model_flags_exist():
    # The two redistribution flags this module and bulk.py gate on are real columns.
    assert {"allows_bulk_export", "allows_api_redistribution"} <= set(Licence.__table__.columns.keys())


def test_batched_subject_lookup_matches_the_single_one(db):
    _lic, src, props = _seed(db, n=2)
    opp = make_visible_opportunity(db, src, public_id_suffix="5")
    events = [make_event(db, p, src) for p in props]
    opp_event = make_event(db, props[0], src, event_type="created")
    opp_event.subject_type, opp_event.subject_id = "opportunity", opp.id
    orphan = make_event(db, props[1], src, event_type="filed")
    orphan.subject_type = "user"
    db.commit()
    everything = [*events, opp_event, orphan]
    batched = subject_infos(db, everything)
    for event in everything:
        assert batched[event.subject_id] == subject_info(db, event) or event is orphan
    assert subject_info(db, orphan)["subject_name"] == "Unknown"
    assert batched[opp.id]["subject_url"].endswith(f"/opportunities/{opp.slug}")


def test_multi_source_record_prints_only_what_bulk_exportable_sources_state(client, db):
    """A record seen in a source whose licence forbids bulk export and in an open one: the row's
    provenance is the open link, every derived column is what the open source states (the other
    source's stored name is not exported under the open source's credit), and only the open source
    is credited. Until 2026-10-06 the row printed the stored values whichever source supplied them
    and credited both (QA-1 / L-10: a value credited to a source that does not state it)."""
    from services.db.models import ProposalSource

    no_bulk = make_attribution_licence(db, id_="no-bulk")
    no_bulk.allows_bulk_export = False
    no_bulk_src = make_public_source(db, no_bulk, id_="us.test.a_no_bulk")
    prop = make_visible_proposal(db, no_bulk_src, public_id_suffix="1")
    prop.name_canonical = "No-Bulk Register Spelling"
    prop.field_provenance = {
        "name_canonical": {
            "source_id": no_bulk_src.id,
            "licence_id": no_bulk.id,
            "retrieved_at": "2026-09-01",
        }
    }
    open_src = make_public_source(db, make_open_licence(db), id_="us.test.b_open")
    db.add(
        ProposalSource(
            proposal_id=prop.id,
            source_id=open_src.id,
            source_record_id="OPEN-1",
            source_url="https://example.org/open#1",
            retrieved_at=dt.datetime.now(UTC),
            licence_id=open_src.licence_id,
            raw={},
            normalised={"name_canonical": "Open Register Spelling"},
            first_seen=dt.datetime.now(UTC),
            last_seen=dt.datetime.now(UTC),
        )
    )
    prop.source_count = 2
    db.commit()
    _login(client, db)
    data = _create(client).json()["data"]
    header, rows = _read_csv(client.get(f"/v1/exports/{data['export_id']}/download").text)
    assert len(rows) == 1
    assert (rows[0]["source_id"], rows[0]["source_record_id"], rows[0]["source_count"]) == (
        "us.test.b_open",
        "OPEN-1",
        "2",
    )
    assert rows[0]["name_canonical"] == "Open Register Spelling"
    assert not any(line.startswith("# us.test.a_no_bulk:") for line in header)
    assert any(line.startswith("# us.test.b_open:") for line in header)
    assert "licences=open-lic" in header[0]
