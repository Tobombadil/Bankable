"""Malformed integer query parameters are a 400 on every route, not a 500 on some.

Two inconsistencies the 2026-09-26 refactor lanes found in code they moved and, being
behaviour-preserving, left alone (`docs/42-backend-review-2026-09-26.md` §8):

- `GET /v1/opportunities/geo` parsed `bbox`/`zoom` bare where `GET /v1/proposals/geo` wrapped the
  same two calls in `try/except ValueError` -> `validation_error`; a malformed value was a 500 on one
  route and a 400 on the other.
- `services/api/admin_records.py` had its own `_int_param` that raised a 400 on a non-integer
  `limit`; the shared `services/api/params.py::int_param` (public, assets, admin-sources routes) let
  `int()` raise.

Both now share one behaviour, pinned here across one route from each family. The assertion is the
API's own problem envelope (`errors.validation_error`, `code`), not just the status code.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.api.conftest import make_open_licence, make_public_source
from tests.conftest import login, make_account, make_user


def _assert_validation_problem(resp, field: str) -> None:  # type: ignore[no-untyped-def]
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "validation_error", body
    assert field in resp.text


@pytest.mark.parametrize(
    "path",
    [
        "/v1/proposals/geo?bbox=abc&zoom=5",
        "/v1/proposals/geo?bbox=-106.6,25.8,-93.5,36.5&zoom=x",
        "/v1/opportunities/geo?bbox=abc&zoom=5",
        "/v1/opportunities/geo?bbox=-106.6,25.8,-93.5,36.5&zoom=x",
    ],
)
def test_malformed_bbox_or_zoom_is_a_400_on_both_geo_routes(client: TestClient, path: str) -> None:
    _assert_validation_problem(client.get(path), "bbox")


@pytest.mark.parametrize("path", ["/v1/proposals?limit=abc", "/v1/assets?limit=abc"])
def test_non_integer_limit_is_a_400_on_public_routes(client: TestClient, path: str) -> None:
    _assert_validation_problem(client.get(path), "limit")


def test_non_integer_limit_is_a_400_on_admin_routes(client: TestClient, db: Session) -> None:
    make_public_source(db, make_open_licence(db))
    account = make_account(db, entitlement="admin", name="Ops")
    user = make_user(db, account, email="ops@example.com", role="operator")
    db.commit()
    login(client, db, user)
    _assert_validation_problem(client.get("/admin/v1/sources?limit=abc"), "limit")
    _assert_validation_problem(client.get("/admin/v1/resolution-candidates?limit=abc"), "limit")
