"""The paid side of "paywall by shape": which shapes are gated, and which do not exist yet.

Owner, 2026-09-19: "alerts, exports, API and watchlists are paid; free users see every record".
With the time delay gone from records, these entitlement gates are the *whole* paywall, so each is
pinned here rather than left implied by the routes' decorators:

* **Alerts and saved searches** need `pro` — a signed-in free account gets `403 forbidden_tier`,
  not an empty list.
* **API keys and webhooks** need `api`; a `pro` account is refused, so the API tier is a real step
  and not a label.
* **Exports and watchlists do not exist.** `GET /v1/me` advertises `exports_per_day` and
  `export_rows_max` quota fields and there is no route behind them, and nothing named watchlist
  exists at all. That is why the owner's Team decision was "sold as seats plus support at the
  current price until export and watchlists exist" (`docs/00-PLAN.md`, 2026-09-18 row item 3), and
  it is asserted here so that the day either ships, this test fails and the claim gets revisited.
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


def test_export_and_watchlist_have_no_routes(client, db) -> None:
    """Two of the four paid shapes are not built. Do not sell them (`docs/41`)."""
    paths = _spec_paths()
    assert [p for p in paths if "export" in p or "watchlist" in p] == []

    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)

    limits = client.get("/v1/me").json()["data"]["limits"]
    assert "exports_per_day" in limits, "the quota field exists..."
    assert "export_rows_max" in limits
    assert limits["exports_per_day"] is None, "...and nothing sets or reads it"
    assert limits["export_rows_max"] is None
