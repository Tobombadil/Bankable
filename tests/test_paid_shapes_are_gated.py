"""The paid side of "paywall by shape": which shapes are gated, and which do not exist yet.

Owner, 2026-09-19: "alerts, exports, API and watchlists are paid; free users see every record".
With the time delay gone from records, these entitlement gates are the *whole* paywall, so each is
pinned here rather than left implied by the routes' decorators:

* **Alerts and saved searches** need `pro` — a signed-in free account gets `403 forbidden_tier`,
  not an empty list.
* **API keys and webhooks** need `api`; a `pro` account is refused, so the API tier is a real step
  and not a label.
* **Watchlists do not exist.** Nothing named watchlist exists at all. That is why the owner's Team
  decision was "sold as seats plus support at the current price until export and watchlists exist"
  (`docs/00-PLAN.md`, 2026-09-18 row item 3), and it is asserted here so that the day watchlists
  ship, this test fails and the claim gets revisited.
* **Exports exist since 2026-09-26 (lane E6b)**, which is the day this file's earlier tripwire
  ("exports have no routes; `GET /v1/me`'s `exports_per_day` is `None`") fired as designed. The
  routes are Pro-gated and `/v1/me` now reports the enforced figures; the Team-pricing claim that
  depended on exports not existing is the owner's to revisit, and `web/pricing.py` still does not
  advertise exports (docs/41) until that decision is made.
"""

from __future__ import annotations

import pytest

from services.api.app import app
from tests.conftest import login, make_account, make_api_key, make_user

PRO_SHAPES = [
    ("get", "/v1/account"),
    ("get", "/v1/saved-searches"),
    ("get", "/v1/alerts"),
]
API_SHAPES = [
    ("get", "/v1/keys"),
    ("get", "/v1/webhooks"),
]


def _spec_paths() -> set[str]:
    return set(app.openapi()["paths"])


@pytest.mark.parametrize(("method", "path"), PRO_SHAPES)
def test_a_free_account_is_refused_the_pro_shapes(client, db, method: str, path: str) -> None:
    account = make_account(db, entitlement="public")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)

    resp = getattr(client, method)(path)
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "forbidden_tier"


@pytest.mark.parametrize(("method", "path"), PRO_SHAPES)
def test_an_anonymous_caller_is_refused_the_pro_shapes(client, method: str, path: str) -> None:
    resp = getattr(client, method)(path)
    assert resp.status_code == 401, resp.text
    assert resp.json()["code"] == "unauthenticated"


@pytest.mark.parametrize(("method", "path"), PRO_SHAPES)
def test_a_pro_account_reaches_the_pro_shapes(client, db, method: str, path: str) -> None:
    """The mirror of the refusals above: an empty 403 everywhere would pass those tests."""
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)

    assert getattr(client, method)(path).status_code == 200


@pytest.mark.parametrize(("method", "path"), API_SHAPES)
def test_a_pro_account_is_refused_the_api_shapes(client, db, method: str, path: str) -> None:
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)

    resp = getattr(client, method)(path)
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "forbidden_tier"


def test_an_api_key_credential_reaches_the_api_shapes(client, db) -> None:
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user, scopes=["read:live", "read:bulk"])
    db.commit()

    resp = client.get("/v1/keys", headers={"Authorization": f"Bearer {secret}"})
    assert resp.status_code == 200


def test_watchlists_have_no_routes_and_exports_are_pro_gated(client, db) -> None:
    """Watchlists are not built; do not sell them (`docs/41`). Exports are, behind `pro`."""
    paths = _spec_paths()
    assert [p for p in paths if "watchlist" in p] == []
    assert {"/v1/exports", "/v1/exports/{export_id}", "/v1/exports/{export_id}/download"} <= paths

    free = make_account(db, entitlement="public", name="Free")
    free_user = make_user(db, free, email="free@example.com")
    db.commit()
    login(client, db, free_user)
    assert client.get("/v1/exports").status_code == 403
    assert client.get("/v1/me").json()["data"]["limits"]["exports_per_day"] is None

    client.cookies.clear()
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    assert client.get("/v1/exports").status_code == 200
    limits = client.get("/v1/me").json()["data"]["limits"]
    assert (limits["exports_per_day"], limits["export_rows_max"]) == (5, 10_000)
    assert limits["bulk_requests_per_hour"] is None, "bulk is a key scope; a session has none"


def test_an_api_key_sees_its_bulk_allowance_on_me(client, db) -> None:
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user, scopes=["read:live", "read:bulk"])
    db.commit()
    limits = client.get("/v1/me", headers={"Authorization": f"Bearer {secret}"}).json()["data"]["limits"]
    assert limits["bulk_requests_per_hour"] == 20
