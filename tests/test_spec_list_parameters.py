"""Spec-vs-code drift on list filters: every query parameter `api/openapi.yaml` documents on a list
operation is either accepted by the code or named in `REFUSED` below, and nothing in `REFUSED` is
silently accepted.

Why this exists (2026-09-27, lane E14): the spec documented `state` on `GET /v1/opportunities` and
`changed_key` / `observed_at[from|to]` on `GET /v1/events` while the code answered
`400 unknown_parameter`, and `GET /v1/events` accepted `q` and applied nothing -- the "accepted and
ignored" class lane E11 had just fixed for `state` on proposals. Nothing compared the two sides, so
both kinds of drift shipped. This test compares them on every `GET` operation that pages (declares
`cursor`), by request, not by reading allowlists: a parameter is *accepted* when a request carrying
it with a plausible value is answered with anything but `400 unknown_parameter`.

`REFUSED` is the honest list of parameters the spec documents and the code does not implement yet
(services/README.md open decision 7): each answers `400 unknown_parameter`, never a silent no-op
(docs/04 API-3). Implementing one means deleting its line here; documenting a new parameter
without implementing it means adding a line here, which is a reviewable decision rather than drift.
The companion check that an accepted filter is *applied* is `tests/test_saved_search_parity.py`
(proposals, opportunities, events) -- acceptance alone does not prove a filter narrows anything.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.api.conftest import (
    make_asset,
    make_event,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.api.ratelimit import default_limiter
from tests.conftest import login, make_account, make_api_key, make_user

SPEC_PATH = pathlib.Path(__file__).resolve().parents[1] / "api" / "openapi.yaml"

#: `(path, parameter)` pairs the spec documents and the code refuses with `400 unknown_parameter`.
#: services/README.md open decision 7 names the same set; keep the two in step. Empty since
#: 2026-09-27 (lane E15): every documented list filter is implemented. A parameter documented
#: before it is built goes here, as a reviewable line, never as a silent no-op.
REFUSED: frozenset[tuple[str, str]] = frozenset()

#: A value per parameter name where the schema alone does not produce a sensible one.
_VALUES: dict[str, str] = {
    "cursor": "",
    "limit": "1",
    "sort": "",
    "include": "count",
    "q": "test",
    "since": "1",
    "subject_id": "prop_0",
    "state": "US-TX",
    "jurisdiction": "US-TX",
    "budget_currency": "EUR",
    "county_fips": "48453",
    "scope": "self",
    "bbox": "-106.6,25.8,-93.5,36.5",
    "zoom": "5",
}


def _spec() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text())
    return loaded


def _resolve(spec: dict[str, Any], param: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in param:
        resolved: dict[str, Any] = spec["components"]["parameters"][param["$ref"].rsplit("/", 1)[-1]]
        return resolved
    return param


def _is_list_shaped(path: str, query: list[dict[str, Any]]) -> bool:
    """A list operation (it pages), or a filtered view of one that takes the list's filters: the
    map payloads (`.../geo`) and the public feeds. The saved-search feed takes a token, not filters."""
    if any(p["name"] == "cursor" for p in query):
        return True
    return path.endswith("/geo") or (path.startswith("/feeds/") and "{rss_token}" not in path)


def list_operations(spec: dict[str, Any]) -> list[tuple[str, str, list[dict[str, Any]]]]:
    """`(path, x-tier, query parameters)` for every list-shaped `GET` operation."""
    out = []
    for path, item in spec["paths"].items():
        op = item.get("get")
        if not op:
            continue
        params = [_resolve(spec, p) for p in [*item.get("parameters", []), *op.get("parameters", [])]]
        query = [p for p in params if p.get("in") == "query"]
        if query and _is_list_shaped(path, query):
            out.append((path, str(op.get("x-tier", "public")), query))
    return out


def _value(param: dict[str, Any]) -> str:
    name = param["name"]
    if name in _VALUES:
        return _VALUES[name]
    schema = param.get("schema") or {}
    if "example" in param:
        return str(param["example"])
    if schema.get("type") == "array":
        schema = schema.get("items") or {}
    if "enum" in schema:
        return str(schema["enum"][0])
    if "oneOf" in schema:
        schema = schema["oneOf"][0]
    kind, fmt = schema.get("type"), schema.get("format")
    if fmt == "date-time":
        return "2026-01-01T00:00:00Z"
    if fmt == "date":
        return "2026-01-01"
    if kind in ("integer", "number"):
        return "1"
    if kind == "boolean":
        return "true"
    return "x"


def _is_unknown_parameter(resp: Any, name: str) -> bool:
    if resp.status_code != 400:
        return False
    body = resp.json()
    return body.get("code") == "unknown_parameter" and any(
        e.get("field") == name for e in body.get("errors") or []
    )


@pytest.fixture()
def world(client: TestClient, db: Session) -> dict[str, Any]:
    """One of everything a list path names, and a caller that clears every tier's gate: an owner
    session (admin, Pro and public operations) and an API key with the bulk scope."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    org = make_org(db)
    prop = make_visible_proposal(db, src, sponsor=org)
    opp = make_visible_opportunity(db, src)
    make_event(db, prop, src)
    # Asset nearby-proposals pages with a cursor since 2026-10-07, so it is walked as a list too.
    asset = make_asset(db, src, lic)
    account = make_account(db, entitlement="api", name="Drift Probe Co")
    user = make_user(db, account, email="owner-drift@example.com", role="owner")
    _key, secret = make_api_key(db, account, user, scopes=["read:live", "read:bulk", "write:webhooks"])
    db.commit()
    login(client, db, user)
    hook = client.post("/v1/webhooks", json={"url": "https://example.org/hook", "types": ["status_change"]})
    assert hook.status_code == 201, hook.text
    return {
        "public_id": {
            "proposals": prop.public_id,
            "opportunities": opp.public_id,
            "organizations": org.public_id,
            "assets": asset.public_id,
        },
        "webhook_id": hook.json()["data"]["webhook_id"],
        "bearer": {"Authorization": f"Bearer {secret}"},
    }


def _concrete(path: str, world: dict[str, Any]) -> str:
    if "{format}" in path:
        return path.replace("{format}", "json")
    if "{public_id}" in path:
        collection = path.split("/")[2]
        return path.replace("{public_id}", world["public_id"][collection])
    return path.replace("{webhook_id}", world["webhook_id"])


def _get(client: TestClient, path: str, tier: str, world: dict[str, Any], params: dict[str, str]) -> Any:
    default_limiter.reset()
    if not path.startswith("/v1/bulk/"):
        return client.get(_concrete(path, world), params=params)
    # The bulk streams are key-only (`read:bulk`): send the key without the session cookie.
    session_cookie = client.cookies.get("session")
    client.cookies.clear()
    try:
        return client.get(_concrete(path, world), params=params, headers=world["bearer"])
    finally:
        client.cookies.set("session", session_cookie or "")


def test_every_documented_list_parameter_is_accepted_or_listed_as_refused(
    client: TestClient, world: dict[str, Any]
) -> None:
    spec = _spec()
    operations = list_operations(spec)
    assert len(operations) >= 30, "the walk found too few list operations to mean anything"
    drift: list[str] = []
    stale: list[str] = []
    for path, tier, params in operations:
        # The probe is only meaningful if an unknown key reaches the endpoint's own check (not a
        # 401/404 raised before it): prove that first, for every operation.
        sentinel = _get(client, path, tier, world, {"not_a_documented_parameter": "1"})
        assert _is_unknown_parameter(sentinel, "not_a_documented_parameter"), (
            f"{path}: an unknown parameter is not refused ({sentinel.status_code} {sentinel.text[:200]})"
        )
        for param in params:
            name = param["name"]
            value = _value(param)
            resp = _get(client, path, tier, world, {name: value} if value else {name: ""})
            refused = _is_unknown_parameter(resp, name)
            listed = (path, name) in REFUSED
            if refused and not listed:
                drift.append(f"{path} documents {name!r}; the code answers 400 unknown_parameter")
            if listed and not refused:
                stale.append(
                    f"{path} {name!r} is listed as refused but the code accepts it ({resp.status_code})"
                )
            assert resp.status_code < 500, f"{path}?{name}={value}: {resp.status_code} {resp.text[:300]}"
    assert not drift, "Spec documents parameters the code refuses:\n" + "\n".join(drift)
    assert not stale, "REFUSED lists parameters the code now accepts:\n" + "\n".join(stale)


def test_refused_list_names_only_documented_parameters() -> None:
    """A `REFUSED` line for a parameter the spec no longer documents is dead weight: remove it."""
    documented = {(path, p["name"]) for path, _tier, params in list_operations(_spec()) for p in params}
    assert set(REFUSED) <= documented, sorted(set(REFUSED) - documented)


def test_no_list_operation_accepts_a_parameter_it_does_not_document(
    client: TestClient, world: dict[str, Any]
) -> None:
    """The other direction: a name another list operation documents, accepted here without being
    documented here, is the "accepted and ignored" shape (`GET /v1/events?q=` until 2026-09-27).
    The candidate names are every query parameter any list operation documents, so a filter copied
    from a sibling allowlist without a spec line and an implementation fails here."""
    operations = list_operations(_spec())
    candidates = {p["name"] for _path, _tier, params in operations for p in params}
    undocumented: list[str] = []
    for path, tier, params in operations:
        documented = {p["name"] for p in params}
        for name in sorted(candidates - documented):
            resp = _get(client, path, tier, world, {name: "x"})
            if not _is_unknown_parameter(resp, name):
                undocumented.append(f"{path} accepts {name!r} ({resp.status_code}) but does not document it")
    assert not undocumented, "\n".join(undocumented)
