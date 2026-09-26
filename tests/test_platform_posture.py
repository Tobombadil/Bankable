"""The platform posture end to end (owner, 2026-09-25; `docs/26-platform-posture.md`).

What has to hold, and is pinned here:

1. **The gates cannot disagree.** The connector registry, the loader, the API predicate, the
   admin publish checks, the coverage statement and the social editorial rule all take their
   class set from `services/posture.py`; under either posture they are one set.
2. **The default is the old behaviour.** With `PLATFORM_POSTURE` unset (this suite), a
   `noncommercial`-licence row is invisible on `public`, `pro` and `api`, refused by the loader
   and refused by the admin publish gate — exactly as `restricted` is.
3. **The switch is the predicate.** With the class set widened to the `noncommercial` posture's
   (the module-level tuple the predicate reads, patched in place, as a restart under the setting
   would produce — `tests/test_visibility_predicate.py` proves the setting produces it), the same
   row is visible on every tier; narrowed again, it is gone. No other change.
4. **`reuse_class` and `allows_commercial_use` agree** (coordinator, 2026-09-25): the CHECK from
   migration 0020 refuses a `noncommercial` licence that claims commercial use, the loader never
   writes one, and the admin reclassification clears the flag on the way in. No route writes the
   flag directly, so the converse — an `open`/`attribution` licence flipped to `false` without a
   class change — has no code path; the `attribution` case is `false` by design (not verified).
5. **`GET /v1/health` reports it**, with the sentence the public pages print.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import types

import pytest
from sqlalchemy.exc import IntegrityError

import pipeline.connectors.registry as registry
import services.api.admin_sources as admin_sources
import services.api.app as api_app
import services.api.coverage as coverage
import services.api.visibility as visibility
import services.db.models as models
import services.ingest.loader as loader
import services.social.editorial as editorial
import web.admin.sources as admin_web_sources
from services import posture
from services.api.conftest import make_event, make_open_licence, make_public_source, make_visible_proposal
from services.db.models import Licence, Source
from tests.conftest import login, make_account, make_api_key, make_user

UTC = dt.UTC
NC_SET = posture.publishable_reuse_classes("noncommercial")


def _reimport(module: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Execute the module's file again as a throwaway module under the current environment.
    Registered in `sys.modules` for the duration (the registry defines a dataclass, and
    `dataclasses` resolves string annotations through `sys.modules[cls.__module__]`); the real
    module is untouched and keeps serving every other test."""
    name = f"{module.__name__}_posture_probe"
    spec = importlib.util.spec_from_file_location(name, module.__file__)
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, probe)
    spec.loader.exec_module(probe)
    return probe


# ------------------------------------------------------------------------ 1. the gates agree
def test_the_vocabulary_is_one_vocabulary() -> None:
    assert models.REUSE_CLASSES == ("open", "attribution", "noncommercial", "restricted", "unknown")
    assert posture._ALL_REUSE_CLASSES == models.REUSE_CLASSES
    assert api_app.REUSE_CLASS_VALUES == list(models.REUSE_CLASSES)


def test_the_connector_gate_and_the_api_gate_cannot_disagree() -> None:
    assert set(registry.PUBLISHABLE_REUSE) == set(visibility.PUBLISHABLE_REUSE_CLASSES)
    assert registry.PUBLISHABLE_REUSE | registry.GATED_REUSE == set(models.REUSE_CLASSES)
    assert registry.PUBLISHABLE_REUSE.isdisjoint(registry.GATED_REUSE)
    assert set(coverage.WITHHELD_REUSE) == set(registry.GATED_REUSE)
    assert editorial.PUBLISHABLE_REUSE_CLASSES == set(visibility.PUBLISHABLE_REUSE_CLASSES)
    assert loader.PUBLISHABLE_REUSE is registry.PUBLISHABLE_REUSE
    assert loader.GATED_REUSE is registry.GATED_REUSE
    # The admin "GATED" chip (web/admin/sources.py D5) derives its set the same way.
    assert set(admin_web_sources.GATED_REUSE_CLASSES) == set(registry.GATED_REUSE)
    assert ("noncommercial" in visibility.PUBLISHABLE_REUSE_CLASSES) == (
        api_app.PLATFORM_POSTURE == "noncommercial"
    )


@pytest.mark.parametrize("value", ["commercial", "noncommercial", "garbage", None])
def test_both_gates_read_the_same_set_under_every_setting(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv(posture.ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(posture.ENV_VAR, value)
    reg = _reimport(registry, monkeypatch)
    vis = _reimport(visibility, monkeypatch)
    expected = set(posture.publishable_reuse_classes(posture.normalise_posture(value)))
    assert set(reg.PUBLISHABLE_REUSE) == set(vis.PUBLISHABLE_REUSE_CLASSES) == expected
    assert reg.PUBLISHABLE_REUSE | reg.GATED_REUSE == set(models.REUSE_CLASSES)
    assert {"restricted", "unknown"} <= reg.GATED_REUSE


# ------------------------------------------------------------------------ 2./3. the predicate
def _nc_licence(db, id_: str = "nc-lic") -> Licence:
    lic = Licence(
        id=id_,
        name="State site noncommercial grant",
        reuse_class="noncommercial",
        attribution_required=True,
        attribution_text="Source: Test State Agency",
        requires_link_back=True,
        allows_derived_publication=True,
        allows_raw_publication=True,
        allows_commercial_use=False,
        gate_flag=False,
        evidence_url="https://example.org/site-policies",
        evidence_retrieved_at=dt.datetime(2026, 9, 25, tzinfo=UTC),
        classified_by="legal-compliance",
    )
    db.add(lic)
    db.flush()
    return lic


def _seed(db) -> dict[str, str]:
    open_src = make_public_source(db, make_open_licence(db))
    shown = make_visible_proposal(db, open_src, public_id_suffix="1")
    shown.name_canonical = "Open Licence Project"
    make_event(db, shown, open_src)

    nc_src = make_public_source(db, _nc_licence(db), id_="us.test.nc_register")
    nc = make_visible_proposal(db, nc_src, public_id_suffix="2")
    nc.name_canonical = "Noncommercial Grant Project"
    nc.min_reuse_class = "noncommercial"
    make_event(db, nc, nc_src)
    db.commit()
    return {"open": shown.name_canonical, "noncommercial": nc.name_canonical}


def _credential(db, entitlement: str) -> dict[str, str]:
    if entitlement == "public":
        return {}
    account = make_account(db, entitlement=entitlement)
    user = make_user(db, account)
    scopes = ["read:live"] + (["read:bulk"] if entitlement == "api" else [])
    _key, secret = make_api_key(db, account, user, scopes=scopes)
    db.commit()
    return {"Authorization": f"Bearer {secret}"}


def _names(client, headers: dict[str, str]) -> set[str]:
    body = client.get("/v1/proposals", headers=headers).json()
    return {row["name_canonical"] for row in body["data"]}


def _event_sources(client, headers: dict[str, str]) -> set[str]:
    body = client.get("/v1/events", headers=headers).json()
    return {(e.get("provenance") or {}).get("source_id") for e in body["data"]}


@pytest.mark.parametrize("entitlement", ["public", "pro", "api"])
def test_under_the_default_posture_a_noncommercial_row_is_invisible_on_every_tier(
    client, db, entitlement: str
) -> None:
    names = _seed(db)
    headers = _credential(db, entitlement)
    returned = _names(client, headers)
    assert names["open"] in returned, "an empty list would pass for the wrong reason"
    assert names["noncommercial"] not in returned
    assert "us.test.nc_register" not in _event_sources(client, headers)
    for path in ("/feeds/proposals.rss", "/feeds/proposals.json", "/feeds/events.rss"):
        assert names["noncommercial"] not in client.get(path).text, path


@pytest.mark.parametrize("entitlement", ["public", "pro", "api"])
def test_under_the_noncommercial_posture_the_same_row_is_visible_and_the_flip_back_hides_it(
    client, db, entitlement: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The switch is the predicate: widen the tuple the predicate reads (what a restart under
    `PLATFORM_POSTURE=noncommercial` yields, per `tests/test_visibility_predicate.py`) and the row
    appears; narrow it and it is gone. Nothing else about the store changes."""
    names = _seed(db)
    headers = _credential(db, entitlement)

    monkeypatch.setattr(visibility, "PUBLISHABLE_REUSE_CLASSES", NC_SET)
    returned = _names(client, headers)
    assert {names["open"], names["noncommercial"]} <= returned
    assert "us.test.nc_register" in _event_sources(client, headers)
    detail = client.get("/v1/proposals?source_id=us.test.nc_register", headers=headers).json()
    assert detail["data"], "the row publishes under the noncommercial posture"
    assert "us.test.nc_register" in {s["source_id"] for s in detail["licence_summary"]["sources"]}

    monkeypatch.setattr(
        visibility, "PUBLISHABLE_REUSE_CLASSES", posture.publishable_reuse_classes("commercial")
    )
    assert names["noncommercial"] not in _names(client, headers)
    assert "us.test.nc_register" not in _event_sources(client, headers)


def test_restricted_and_unknown_stay_invisible_under_the_noncommercial_posture(
    client, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Widening to `noncommercial` widens to exactly that class. PJM named, as in every other
    licence-gate test."""
    _seed(db)
    lic = Licence(id="pjm-restricted", name="PJM DM2", reuse_class="restricted", gate_flag=True)
    db.add(lic)
    db.flush()
    pjm = make_public_source(db, lic, id_="us.iso.pjm.gen_queue")
    prop = make_visible_proposal(db, pjm, public_id_suffix="9")
    prop.name_canonical = "PJM Queue Project"
    prop.min_reuse_class = "restricted"
    db.commit()
    monkeypatch.setattr(visibility, "PUBLISHABLE_REUSE_CLASSES", NC_SET)
    assert "PJM Queue Project" not in _names(client, {})
    assert client.get("/v1/proposals?source_id=us.iso.pjm.gen_queue").json()["data"] == []


# ------------------------------------------------------------------------ 4. class <-> flag
def test_a_noncommercial_licence_may_not_claim_commercial_use(db) -> None:
    db.add(Licence(id="nc-bad", name="x", reuse_class="noncommercial", allows_commercial_use=True))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    ok = Licence(id="nc-ok", name="x", reuse_class="noncommercial", allows_commercial_use=False)
    db.add(ok)
    db.flush()
    assert ok.is_publishable_class is False, "under the default posture the class is gated"
    src = make_public_source(db, ok, id_="us.test.nc_gated")
    assert src.gated is True


def test_the_orm_property_follows_the_posture(db, monkeypatch: pytest.MonkeyPatch) -> None:
    lic = _nc_licence(db)
    assert lic.is_publishable_class is False and lic.gate_clear is False
    monkeypatch.setenv(posture.ENV_VAR, "noncommercial")
    assert lic.is_publishable_class is True and lic.gate_clear is True
    # `restricted` never clears, whatever the posture.
    r = Licence(id="r", name="r", reuse_class="restricted", gate_flag=False)
    assert r.is_publishable_class is False


def test_admin_reclassification_to_noncommercial_clears_the_commercial_flag(client, db) -> None:
    lic = make_open_licence(db)  # allows_commercial_use=True
    assert lic.allows_commercial_use is True
    account = make_account(db, entitlement="admin", name="Legal")
    legal = make_user(db, account, email="legal-posture@example.com", role="legal")
    db.commit()
    login(client, db, legal)

    resp = client.put(
        f"/admin/v1/licences/{lic.id}/gate",
        json={
            "gate_flag": False,
            "reuse_class": "noncommercial",
            "reason": "terms read: noncommercial grant",
        },
    )
    assert resp.status_code == 200, resp.text
    new_id = resp.json()["data"]["licence_id"]
    new = db.get(Licence, new_id)
    db.refresh(new)
    assert new.reuse_class == "noncommercial"
    assert new.allows_commercial_use is False, "the class and the flag agree by construction"
    # And back the other way the flag is not invented: it stays whatever the row recorded.
    resp = client.put(
        f"/admin/v1/licences/{new_id}/gate",
        json={"gate_flag": False, "reuse_class": "attribution", "reason": "terms re-read"},
    )
    assert resp.status_code == 200, resp.text
    back = db.get(Licence, resp.json()["data"]["licence_id"])
    db.refresh(back)
    assert back.allows_commercial_use is False


def test_no_admin_route_writes_allows_commercial_use_directly() -> None:
    """The converse guarantee is structural: the only writers of the flag are the loader (from
    the manifest class) and the reclassification copy above. If a route ever accepts it, this
    test is where the consistency rule has to be re-argued."""
    import inspect

    for module in (admin_sources,):
        source = inspect.getsource(module)
        assert 'body.get("allows_commercial_use")' not in source
        assert 'body["allows_commercial_use"]' not in source


# ------------------------------------------------------------------------ the loader
def _nc_entry(id_: str = "us.test.nc_source") -> registry.SourceEntry:
    return registry.SourceEntry.from_yaml(
        {
            "id": id_,
            "name": "Test State Register",
            "jurisdiction": "US-TX",
            "category": "permit",
            "operator": "Test State Agency",
            "url": "https://example.org/register",
            "access": "pdf",
            "reuse": "noncommercial",
            "publication": "raw_ok",
            "cadence": "weekly",
            "tier": 2,
            "license": "permission to copy and distribute the information ... for noncommercial use",
        }
    )


def test_the_loader_refuses_a_noncommercial_source_under_the_default_posture(db) -> None:
    with pytest.raises(loader.GateRefused):
        loader.upsert_licence_and_source(db, _nc_entry(), "2026-09-25")


def test_the_loader_writes_a_consistent_noncommercial_licence_under_the_noncommercial_posture(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loader, "PUBLISHABLE_REUSE", frozenset(NC_SET))
    monkeypatch.setattr(loader, "GATED_REUSE", frozenset(posture.gated_reuse_classes("noncommercial")))
    source = loader.upsert_licence_and_source(db, _nc_entry(), "2026-09-25")
    db.flush()
    lic = source.licence
    assert lic.reuse_class == "noncommercial"
    assert lic.allows_commercial_use is False
    assert lic.attribution_required is True and lic.requires_link_back is True
    assert lic.attribution_text == "Source: Test State Agency"
    assert lic.allows_raw_publication is True and lic.allows_derived_publication is True
    assert lic.allows_bulk_export is False and lic.allows_api_redistribution is False
    assert source.publish_state == "public"


# ------------------------------------------------------------------------ 5. health
def test_health_reports_the_posture_and_its_sentence(client, db) -> None:
    body = client.get("/v1/health").json()
    assert body["posture"] == api_app.PLATFORM_POSTURE == "commercial"
    assert body["posture_statement"] == posture.posture_statement("commercial")
    assert body["posture_statement"].startswith("This platform operates under a commercial posture")


def test_the_admin_publish_gate_names_the_posture_set(db) -> None:
    lic = _nc_licence(db)
    missing = admin_sources._licence_gate_missing(lic)
    assert any("reuse_class" in m and "platform posture" in m for m in missing), missing
    assert not any(m.startswith("legal evidence") for m in missing)


def test_source_gated_property_agrees_with_the_registry_for_the_default_posture(db) -> None:
    open_src = make_public_source(db, make_open_licence(db))
    assert open_src.gated is False
    assert Source.gated.fget is not None
