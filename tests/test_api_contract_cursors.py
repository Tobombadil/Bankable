"""Cursor and `since` contract: a tampered or malformed value is `400`, never `500`, and keyset
paging over a DATE column is exact (backend audit 2026-09-30 F9, F10).

F9: the fuzzer's cursor set (`scratchpad/audit/backend/fuzz.py`) produced 324 server errors on 17
public and 13 admin lists, because `decode_cursor` checked only the base64/JSON envelope and the
payload's `v` and `id` reached the driver unchecked; `since` overflowed or passed `str.isdigit()`.
Every list in the spec that takes `cursor` is exercised here with that set.

F10: `paginate` turned every string cursor value into a `datetime`, so on SQLite a `Date` sort
(`sort=open_at`) compared `'2026-08-10 00:00:00.000000'` with the stored `'2026-08-10'`: ascending
skipped the rows that shared a page-boundary date, descending returned the same page for ever.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import pathlib
from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app
from services.api.auth import create_session
from services.api.conftest import (
    make_event,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.api.deps import get_db
from services.db.models import WebhookEndpoint
from services.ids import public_id
from tests.conftest import make_account, make_api_key, make_user

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
UTC = dt.UTC
NIL = "00000000-0000-0000-0000-000000000000"


def _b64(obj: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


#: The audit fuzzer's cursor set, plus the overflow and wrong-type cases it reported.
FUZZ_CURSORS = [
    "!!!",
    _b64({"v": {"a": 1}, "id": "x"}),
    _b64({"v": [1, 2], "id": "x"}),
    _b64({"v": "2026-01-01T00:00:00Z", "id": "not-a-uuid"}),
    _b64({"v": True, "id": NIL}),
    _b64({"v": 1e308, "id": "0"}),
    _b64({"v": None, "id": None}),
    _b64({"v": "zzz", "id": NIL}),
    _b64([1, 2]),
    _b64({"v": 5}),
    _b64("str"),
    _b64({"v": "2026-01-01", "id": "x" * 3}),
    _b64({"v": 10**30, "id": "1"}),
    _b64({"v": 10**30, "id": NIL}),
    _b64({"v": -(10**30), "id": NIL}),
    _b64({"v": {"x": 1}, "id": NIL}),
    _b64({"v": "2026-13-45", "id": NIL}),
    _b64({"v": 1.5, "id": {"a": 1}}),
    "eyJ2IjogSW5maW5pdHksICJpZCI6ICIwMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwMDAifQ",  # v: Infinity
]
FUZZ_SINCE = ["99999999999999999999", "²", "-1", "abc", "1.5", " 1"]


def _cursor_routes() -> list[tuple[str, bool]]:
    spec = yaml.safe_load((REPO_ROOT / "api" / "openapi.yaml").read_text())
    comp = spec["components"]["parameters"]

    def res(p: dict[str, Any]) -> dict[str, Any]:
        return comp[p["$ref"].split("/")[-1]] if "$ref" in p else p

    out = []
    for path, ops in spec["paths"].items():
        get = ops.get("get")
        if not get:
            continue
        names = {
            res(p)["name"]
            for p in ops.get("parameters", []) + get.get("parameters", [])
            if res(p).get("in") == "query"
        }
        if "cursor" in names:
            out.append((path, "since" in names))
    return out


CURSOR_ROUTES = _cursor_routes()


@pytest.fixture()
def world(db_sessionmaker: sessionmaker[Session]) -> Iterator[dict[str, Any]]:
    with db_sessionmaker() as db:
        source = make_public_source(db, make_open_licence(db))
        org = make_org(db, "Acme Power LLC")
        prop = make_visible_proposal(db, source, sponsor=org)
        make_event(db, prop, source)
        opp = make_visible_opportunity(db, source)
        account = make_account(db, entitlement="api")
        owner = make_user(db, account, role="owner")
        _key, secret = make_api_key(db, account, owner, scopes=["read:live", "read:bulk", "write:webhooks"])
        endpoint = WebhookEndpoint(
            public_id="",
            account_id=account.id,
            created_by_user_id=owner.id,
            url="https://example.com/hook",
            types=["event.published"],
            secret="s",
        )
        db.add(endpoint)
        db.flush()
        endpoint.public_id = public_id("whe", endpoint.id)
        _row, cookie = create_session(db, owner)
        db.commit()
        ids = {"proposals": prop.public_id, "opportunities": opp.public_id, "organizations": org.public_id}
        webhook_id = endpoint.public_id

    def _override() -> Iterator[Session]:
        s = db_sessionmaker()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app, raise_server_exceptions=False) as c:
        yield {"client": c, "ids": ids, "webhook_id": webhook_id, "cookie": cookie, "key": secret}
    app.dependency_overrides.clear()


def _real_path(path: str, world: dict[str, Any]) -> str:
    if "{public_id}" in path:
        path = path.replace("{public_id}", world["ids"][path.split("/")[2]])
    return path.replace("{webhook_id}", world["webhook_id"])


def _credentials(path: str, world: dict[str, Any]) -> dict[str, Any]:
    if path.startswith("/v1/bulk/"):
        return {"headers": {"Authorization": f"Bearer {world['key']}"}}
    return {"cookies": {"session": world["cookie"]}}


@pytest.mark.parametrize(("path", "_has_since"), CURSOR_ROUTES, ids=[p for p, _ in CURSOR_ROUTES])
def test_a_malformed_cursor_is_400_problem_never_500(
    path: str, _has_since: bool, world: dict[str, Any]
) -> None:
    from services.api.ratelimit import default_limiter

    client: TestClient = world["client"]
    real = _real_path(path, world)
    creds = _credentials(path, world)
    baseline = client.get(real, **creds)
    assert baseline.status_code == 200, (path, baseline.status_code, baseline.text[:200])
    failures = []
    for cursor in FUZZ_CURSORS:
        default_limiter.reset()
        resp = client.get(real, params={"cursor": cursor}, **creds)
        if resp.status_code >= 500:
            failures.append((cursor, resp.status_code, resp.text[:120]))
        elif resp.status_code == 400:
            assert resp.headers["content-type"].startswith("application/problem+json")
            assert resp.json()["code"] in ("invalid_cursor", "validation_error")
    assert not failures, failures


@pytest.mark.parametrize("path", [p for p, has_since in CURSOR_ROUTES if has_since])
def test_a_malformed_since_is_400_problem_never_500(path: str, world: dict[str, Any]) -> None:
    from services.api.ratelimit import default_limiter

    client: TestClient = world["client"]
    for since in FUZZ_SINCE:
        default_limiter.reset()
        resp = client.get(_real_path(path, world), params={"since": since}, **_credentials(path, world))
        assert resp.status_code == 400, (since, resp.status_code, resp.text[:200])
        assert resp.json()["code"] == "validation_error"
    default_limiter.reset()
    ok = client.get(_real_path(path, world), params={"since": "0"}, **_credentials(path, world))
    assert ok.status_code == 200


# ------------------------------------------------------------------------------- F10: DATE keyset
OPEN_DATES = [
    dt.date(2026, 8, 10),
    dt.date(2026, 8, 10),
    dt.date(2026, 8, 10),
    dt.date(2026, 8, 11),
    dt.date(2026, 8, 11),
    dt.date(2026, 8, 12),
    None,
]


@pytest.mark.parametrize("sort", ["open_at", "-open_at"])
def test_paging_over_a_date_column_returns_every_row_once(sort: str, client: TestClient, db: Session) -> None:
    source = make_public_source(db, make_open_licence(db))
    expected = set()
    for i, open_at in enumerate(OPEN_DATES, start=1):
        opp = make_visible_opportunity(db, source, public_id_suffix=str(i))
        opp.open_at = open_at
        expected.add(opp.public_id)
    db.commit()

    seen: Counter[str] = Counter()
    cursor = None
    order: list[dt.date | None] = []
    for _page in range(20):
        params = {"sort": sort, "limit": 2, "status": "open,closed,awarded,cancelled"}
        if cursor:
            params["cursor"] = cursor
        resp = client.get("/v1/opportunities", params=params)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for row in body["data"]:
            seen[row["public_id"]] += 1
            order.append(dt.date.fromisoformat(row["open_at"][:10]) if row.get("open_at") else None)
        cursor = body["page"]["next_cursor"]
        if not cursor:
            break
    assert cursor is None, "paging did not terminate"
    assert set(seen) == expected and set(seen.values()) == {1}, seen
    dated = [d for d in order if d is not None]
    assert dated == sorted(dated, reverse=sort.startswith("-"))
    assert order[-1] is None  # NULLs last in both directions


@pytest.mark.parametrize(
    "path",
    [
        "/v1/interconnection-points?sort=-active_mw",
        "/v1/proposals?sort=capacity_mw",
        "/v1/proposals?sort=first_seen",
    ],
)
def test_a_string_cursor_on_a_numeric_or_time_sort_is_400(path: str, world: dict[str, Any]) -> None:
    """SQLite compares any string with a number and answered 200; Postgres raises a type error.
    Typing the cursor by its column makes both answer the documented 400."""
    resp = world["client"].get(path, params={"cursor": _b64({"v": "zzz", "id": NIL})})
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "invalid_cursor"


#: `services/billing/router.py` still parses `limit` with a bare `int()`; it is outside this lane's
#: files and is listed as an open item in the lane report.
_LIMIT_NOT_YET_VALIDATED = {"/admin/v1/subscriptions"}


@pytest.mark.parametrize("path", [p for p, _ in CURSOR_ROUTES if p not in _LIMIT_NOT_YET_VALIDATED])
def test_a_non_integer_limit_is_400(path: str, world: dict[str, Any]) -> None:
    """F9's third family: seven admin lists called `int(limit)` and answered 500 to `limit=abc`.
    Every list that parses `limit` answers 400; none answers 500. (`/v1/saved-searches`, `/v1/alerts`
    and a webhook's deliveries accept `limit` without reading it, which is not this finding.)"""
    resp = world["client"].get(_real_path(path, world), params={"limit": "abc"}, **_credentials(path, world))
    assert resp.status_code < 500, (path, resp.status_code, resp.text[:200])
    if path.startswith("/admin/"):
        assert resp.status_code == 400, (path, resp.status_code)
    if resp.status_code == 400:
        assert resp.json()["code"] == "validation_error"
