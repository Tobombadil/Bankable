"""Removing the time delay must not make one restricted row visible.

The 2026-09-19 owner decision ("free users see every record") changed *when* `public_at` falls,
nothing else. `services/api/visibility.py`'s docstring is explicit that `licence_permits` is
unchanged across tiers and that `restricted`/`unknown` sources stay invisible on every non-admin
surface; `CLAUDE.md` makes PJM specifically non-public until a licence exists. This file is the
proof, written so that it fails if the licence clause is ever loosened -- at three levels:

1. **Structural.** The licence clause of the predicate is identical for `public`, `pro` and `api`.
   Adding `restricted` to `PUBLISHABLE_REUSE_CLASSES`, or giving one tier a wider set, fails here.
2. **Behavioural, through the real API.** A restricted-licence proposal and an unknown-licence
   proposal, both `publish_state = 'public'` with `public_at` a day in the past -- i.e. rows for
   which the *time* gate is wide open -- are absent from `GET /v1/proposals` on the public, Pro
   and API tiers, and so are their events and the feeds. An open-licence row in the same store is
   present, so an empty result cannot pass this test by accident.
3. **Named.** The same, for a source id of `us.iso.pjm.gen_queue`: PJM now carries a change-event
   lag flag in the manifest like every other ISO queue, and that must not be mistaken anywhere for
   permission to publish it.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from services.api.conftest import (
    make_event,
    make_open_licence,
    make_public_source,
    make_visible_proposal,
)
from services.api.visibility import (
    PUBLISHABLE_REUSE_CLASSES,
    proposal_visibility_filter,
)
from services.db.models import REUSE_CLASSES, Licence, Proposal, Source
from tests.conftest import make_account, make_api_key, make_user

UTC = dt.UTC
GATED_CLASSES = ("restricted", "unknown")


def _gated_source(db, reuse_class: str, source_id: str) -> Source:
    """A source whose *licence* is gated but whose every other gate is wide open: the source is
    `publish_state = 'public'`, so only `licence_permits` can exclude it."""
    lic = Licence(
        id=f"{reuse_class}-lic-{source_id}",
        name=f"{reuse_class.title()} terms",
        reuse_class=reuse_class,
        gate_flag=True,
        allows_derived_publication=True,
        allows_raw_publication=False,
    )
    db.add(lic)
    db.flush()
    src = Source(
        id=source_id,
        name=f"{reuse_class} source",
        category="generation_queue",
        url=f"https://example.org/{reuse_class}",
        access="html",
        cadence="weekly",
        licence_id=lic.id,
        publish_state="public",
    )
    db.add(src)
    db.flush()
    return src


def _seed(db) -> dict[str, str]:
    """One visible open-licence proposal plus one per gated class, all with `public_at` in the
    past so the *time* gate admits every one of them."""
    now = dt.datetime.now(UTC)
    yesterday = now - dt.timedelta(days=1)

    open_src = make_public_source(db, make_open_licence(db))
    shown = make_visible_proposal(db, open_src, public_id_suffix="1")
    shown.name_canonical = "Open Licence Project"
    make_event(db, shown, open_src)

    names = {"open": shown.name_canonical}
    for i, (reuse_class, source_id) in enumerate(
        [("restricted", "us.iso.pjm.gen_queue"), ("unknown", "us.iso.miso.gen_queue")], start=2
    ):
        src = _gated_source(db, reuse_class, source_id)
        prop = make_visible_proposal(db, src, public_id_suffix=str(i))
        prop.name_canonical = f"{reuse_class.title()} Source Project"
        prop.min_reuse_class = reuse_class
        prop.public_at = yesterday
        prop.published_at = yesterday
        make_event(db, prop, src)
        names[reuse_class] = prop.name_canonical
    db.commit()
    return names


def _credential(db, client, entitlement: str) -> dict[str, str]:
    if entitlement == "public":
        return {}
    account = make_account(db, entitlement=entitlement)
    user = make_user(db, account)
    scopes = ["read:live"] + (["read:bulk"] if entitlement == "api" else [])
    _key, secret = make_api_key(db, account, user, scopes=scopes)
    db.commit()
    return {"Authorization": f"Bearer {secret}"}


# ------------------------------------------------------------------------ 1. structural
def test_the_licence_clause_is_identical_on_every_tier() -> None:
    """Compiled with `literal_binds`, so the *values* are compared and not just the SQL shape:
    `IN (__[POSTCOMPILE_...])` renders identically whatever the list holds, and a per-tier widening
    would slip past a plain `str()` of the clause."""
    clauses = {
        tier: str(
            proposal_visibility_filter(tier, dt.datetime(2026, 9, 20, tzinfo=UTC))[3].compile(
                compile_kwargs={"literal_binds": True}
            )
        )
        for tier in ("public", "pro", "api")
    }
    assert len(set(clauses.values())) == 1, clauses
    rendered = next(iter(clauses.values()))
    assert "min_reuse_class" in rendered
    assert not any(gated in rendered for gated in GATED_CLASSES), rendered


def test_only_open_and_attribution_are_publishable() -> None:
    assert set(PUBLISHABLE_REUSE_CLASSES) == {"open", "attribution"}
    assert set(GATED_CLASSES) <= set(REUSE_CLASSES)
    assert set(PUBLISHABLE_REUSE_CLASSES).isdisjoint(GATED_CLASSES)


# ------------------------------------------------------------------------ 2. behavioural
@pytest.mark.parametrize("entitlement", ["public", "pro", "api"])
def test_gated_records_are_invisible_on_every_non_admin_tier(client, db, entitlement: str) -> None:
    names = _seed(db)
    headers = _credential(db, client, entitlement)

    body = client.get("/v1/proposals", headers=headers).json()
    assert body["data"], "an empty list would pass this test for the wrong reason"
    returned = {row["name_canonical"] for row in body["data"]}
    assert names["open"] in returned
    assert names["restricted"] not in returned
    assert names["unknown"] not in returned


@pytest.mark.parametrize("entitlement", ["public", "pro", "api"])
def test_gated_events_are_invisible_on_every_non_admin_tier(client, db, entitlement: str) -> None:
    names = _seed(db)
    headers = _credential(db, client, entitlement)

    body = client.get("/v1/events", headers=headers).json()
    leaked = [
        e for e in body["data"] if e.get("subject", {}).get("name") in (names["restricted"], names["unknown"])
    ]
    assert leaked == []
    sources = {(e.get("provenance") or {}).get("source_id") for e in body["data"]}
    assert sources.isdisjoint({"us.iso.pjm.gen_queue", "us.iso.miso.gen_queue"})


def test_gated_records_never_reach_the_public_feeds(client, db) -> None:
    names = _seed(db)
    for path in ("/feeds/proposals.rss", "/feeds/proposals.json", "/feeds/events.rss"):
        text = client.get(path).text
        assert names["restricted"] not in text, path
        assert names["unknown"] not in text, path


# ------------------------------------------------------------------------ 3. named: PJM
def test_pjm_is_not_public_even_though_it_now_carries_a_change_event_lag(client, db) -> None:
    """`data/sources.yaml` gives `us.iso.pjm.gen_queue` a `change_event_lag_days` like every other
    ISO queue. That field says *how long its change events would wait if it were publishable*; it
    is not, and nothing about narrowing the lag changes that (`CLAUDE.md`)."""
    _seed(db)
    stored = db.scalars(select(Proposal).join(Source, Source.id == "us.iso.pjm.gen_queue")).all()
    assert stored, "the PJM row is in the store -- it is the API that must withhold it"

    body = client.get("/v1/proposals?source_id=us.iso.pjm.gen_queue").json()
    assert body["data"] == []
    assert "us.iso.pjm.gen_queue" not in {s["source_id"] for s in body["licence_summary"]["sources"]}
