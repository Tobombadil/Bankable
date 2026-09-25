"""Schedule slippage: the computation, its SQL twin, the filters and the serialised field
(`services/api/slippage.py`, docs/22 §18).

The load-bearing cases, each of which is a way the signal could lie rather than a way it could
crash: a proposal with no target date must never count as slipped; a target date of exactly today
must not either; and the SQL and Python twins must agree on every boundary, because one decides
what a filter returns and the other decides what the row then says about itself.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from services.api.conftest import (
    make_open_licence,
    make_public_source,
    make_visible_proposal,
)
from services.api.serialize import serialize_proposal
from services.api.slippage import (
    SLIP_ACTIVE_STATES,
    SLIP_BUCKETS,
    SLIP_GRACE_DAYS,
    proposal_slip,
    slip_bucket,
    slip_days,
    slip_filter,
    today,
)
from services.db.models import Proposal

ON = dt.date(2026, 9, 21)


@pytest.fixture(autouse=True)
def _clock_pinned_to_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this module runs on `ON`, including the ones that go through the API.

    `slip_fixtures` anchors its target dates to the literal `ON`, but the list endpoint filters
    on the real clock (`services/api/app.py` imports `today` as `slip_today`), so the `at_grace`
    row -- exactly `SLIP_GRACE_DAYS` late on `ON` -- crossed the grace line the day after the
    file was written. CI was green on 2026-09-21 by coincidence of date and red from
    2026-09-22 on `main` itself. Two patch targets because `app.py` binds the name at import:
    patching only `services.api.slippage.today` would fix `proposal_slip` and leave the filter
    on the wall clock.
    """
    monkeypatch.setattr("services.api.slippage.today", lambda: ON)
    monkeypatch.setattr("services.api.app.slip_today", lambda: ON)


def _now() -> dt.date:
    """Today's real date: the `serialize_proposal` tests exercise the production default clock."""
    return dt.datetime.now(dt.UTC).date()


# ------------------------------------------------------------------- the computation itself
def test_no_target_date_is_never_slipped() -> None:
    """The rule the whole signal rests on: nothing was promised, so nothing is late."""
    assert slip_days("under_construction", None, on=ON) is None


def test_target_date_exactly_today_is_not_slipped() -> None:
    """A date is not missed on the day it names."""
    assert slip_days("under_construction", ON, on=ON) is None


def test_future_target_date_is_not_slipped() -> None:
    assert slip_days("filed", ON + dt.timedelta(days=400), on=ON) is None


@pytest.mark.parametrize(
    ("late", "expected"),
    [
        (SLIP_GRACE_DAYS - 1, None),
        (SLIP_GRACE_DAYS, None),  # the grace boundary is inclusive: 90 days past is not yet late
        (SLIP_GRACE_DAYS + 1, SLIP_GRACE_DAYS + 1),
    ],
)
def test_grace_boundary(late: int, expected: int | None) -> None:
    assert slip_days("permitted", ON - dt.timedelta(days=late), on=ON) == expected


@pytest.mark.parametrize(
    ("days", "bucket"),
    [(91, "under_1y"), (365, "under_1y"), (366, "1_to_3y"), (1095, "1_to_3y"), (1096, "over_3y")],
)
def test_bucket_boundaries(days: int, bucket: str) -> None:
    assert slip_bucket(days) == bucket


@pytest.mark.parametrize("state", ["built", "withdrawn", "cancelled", "unknown"])
def test_inactive_states_are_never_slipped(state: str) -> None:
    """A built project's target date is spent and a dead one is not late (docs/22 §18.2)."""
    assert slip_days(state, ON - dt.timedelta(days=2000), on=ON) is None


@pytest.mark.parametrize("state", SLIP_ACTIVE_STATES)
def test_every_active_state_can_slip(state: str) -> None:
    assert slip_days(state, ON - dt.timedelta(days=200), on=ON) == 200


def test_today_is_a_utc_date() -> None:
    assert today() == dt.datetime.now(dt.UTC).date()


def test_active_states_match_the_web_default_view() -> None:
    """`SLIP_ACTIVE_STATES` is duplicated rather than imported (services must not import web);
    if the two ever diverge, a row hidden from the default list could still be flagged on it."""
    from web.viewmodels import ACTIVE_PROPOSAL_STATES

    assert SLIP_ACTIVE_STATES == ACTIVE_PROPOSAL_STATES


# ------------------------------------------------------------------------------ the payload
def test_proposal_slip_payload(db: Session) -> None:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(
        db, src, lifecycle_state="permitted", proposed_online_date=ON - dt.timedelta(days=500)
    )
    assert proposal_slip(prop, on=ON) == {
        "target_date": (ON - dt.timedelta(days=500)).isoformat(),
        "days_late": 500,
        "bucket": "1_to_3y",
        "grace_days": SLIP_GRACE_DAYS,
    }


def test_proposal_slip_is_none_without_a_date(db: Session) -> None:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, lifecycle_state="under_construction")
    assert prop.proposed_online_date is None
    assert proposal_slip(prop, on=ON) is None


def test_serialize_proposal_carries_schedule_slip(db: Session) -> None:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    slipped = make_visible_proposal(
        db,
        src,
        public_id_suffix="1",
        lifecycle_state="studied",
        proposed_online_date=_now() - dt.timedelta(days=400),
    )
    on_time = make_visible_proposal(
        db,
        src,
        public_id_suffix="2",
        lifecycle_state="studied",
        proposed_online_date=_now() + dt.timedelta(days=400),
    )
    assert serialize_proposal(slipped)["schedule_slip"]["bucket"] == "1_to_3y"
    assert serialize_proposal(on_time)["schedule_slip"] is None


# --------------------------------------------------------------------------- the SQL twin
def _ids(db: Session, clause: sa.ColumnElement[bool]) -> set[str]:
    return {p.public_id for p in db.scalars(sa.select(Proposal).where(clause)).all()}


@pytest.fixture()
def slip_fixtures(db: Session) -> dict[str, Proposal]:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    made = {
        "recent": (10, "under_construction"),
        "at_grace": (SLIP_GRACE_DAYS, "under_construction"),
        "just_over": (SLIP_GRACE_DAYS + 1, "under_construction"),
        "year": (300, "permitted"),
        "three": (800, "filed"),
        "ancient": (2000, "contracted"),
        "dead": (2000, "withdrawn"),
    }
    out: dict[str, Proposal] = {}
    for i, (name, (late, state)) in enumerate(made.items()):
        out[name] = make_visible_proposal(
            db,
            src,
            public_id_suffix=str(i + 1),
            lifecycle_state=state,
            proposed_online_date=ON - dt.timedelta(days=late),
        )
    out["undated"] = make_visible_proposal(
        db, src, public_id_suffix="99", lifecycle_state="under_construction"
    )
    db.flush()
    return out


def test_sql_twin_agrees_with_the_python_twin(slip_fixtures: dict[str, Proposal], db: Session) -> None:
    """The one invariant that matters across the two implementations."""
    by_python = {
        p.public_id
        for p in slip_fixtures.values()
        if slip_days(p.lifecycle_state, p.proposed_online_date, on=ON) is not None
    }
    assert _ids(db, slip_filter(slipped=True, buckets=None, on=ON)) == by_python
    assert by_python == {slip_fixtures[k].public_id for k in ("just_over", "year", "three", "ancient")}


def test_slipped_false_keeps_undated_rows(slip_fixtures: dict[str, Proposal], db: Session) -> None:
    """Regression for SQL three-valued logic: `NOT (NULL < date)` is NULL, which would drop every
    proposal with no target date out of the complement instead of keeping it."""
    not_slipped = _ids(db, slip_filter(slipped=False, buckets=None, on=ON))
    assert slip_fixtures["undated"].public_id in not_slipped
    assert slip_fixtures["dead"].public_id in not_slipped
    assert slip_fixtures["at_grace"].public_id in not_slipped
    assert slip_fixtures["ancient"].public_id not in not_slipped


def test_buckets_partition_the_slipped_set(slip_fixtures: dict[str, Proposal], db: Session) -> None:
    all_slipped = _ids(db, slip_filter(slipped=True, buckets=None, on=ON))
    per_bucket = [_ids(db, slip_filter(slipped=None, buckets=[b], on=ON)) for b in SLIP_BUCKETS]
    assert set().union(*per_bucket) == all_slipped
    assert sum(len(s) for s in per_bucket) == len(all_slipped)  # disjoint
    assert per_bucket[0] == {slip_fixtures[k].public_id for k in ("just_over", "year")}
    assert per_bucket[1] == {slip_fixtures["three"].public_id}
    assert per_bucket[2] == {slip_fixtures["ancient"].public_id}


# ------------------------------------------------------------------------------ the filters
def test_filter_slipped_true(client, db, slip_fixtures) -> None:  # type: ignore[no-untyped-def]
    db.commit()
    body = client.get("/v1/proposals", params={"slipped": "true", "limit": 100}).json()
    assert {r["public_id"] for r in body["data"]} == {
        slip_fixtures[k].public_id for k in ("just_over", "year", "three", "ancient")
    }
    assert all(r["schedule_slip"] is not None for r in body["data"])


def test_filter_slipped_false_includes_undated(client, db, slip_fixtures) -> None:  # type: ignore[no-untyped-def]
    db.commit()
    body = client.get("/v1/proposals", params={"slipped": "false", "limit": 100}).json()
    ids = {r["public_id"] for r in body["data"]}
    assert slip_fixtures["undated"].public_id in ids
    assert slip_fixtures["ancient"].public_id not in ids


def test_filter_slip_bucket_csv(client, db, slip_fixtures) -> None:  # type: ignore[no-untyped-def]
    db.commit()
    body = client.get("/v1/proposals", params={"slip_bucket": "1_to_3y,over_3y", "limit": 100}).json()
    assert {r["public_id"] for r in body["data"]} == {
        slip_fixtures[k].public_id for k in ("three", "ancient")
    }


def test_unknown_slip_bucket_is_a_400_naming_the_token(client, db, slip_fixtures) -> None:  # type: ignore[no-untyped-def]
    db.commit()
    resp = client.get("/v1/proposals", params={"slip_bucket": "over_3y,last_tuesday"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "validation_error"
    assert "last_tuesday" in body["detail"]
    assert body["errors"][0]["field"] == "slip_bucket"


def test_unknown_slipped_value_is_a_400(client, db, slip_fixtures) -> None:  # type: ignore[no-untyped-def]
    db.commit()
    resp = client.get("/v1/proposals", params={"slipped": "yes"})
    assert resp.status_code == 400
    assert resp.json()["errors"][0]["field"] == "slipped"


def test_slipped_false_with_a_bucket_is_a_400_not_an_empty_page(client, db, slip_fixtures) -> None:  # type: ignore[no-untyped-def]
    db.commit()
    resp = client.get("/v1/proposals", params={"slipped": "false", "slip_bucket": "over_3y"})
    assert resp.status_code == 400
    assert "cannot be combined" in resp.json()["detail"]


def test_slipped_true_with_a_bucket_narrows_to_the_bucket(client, db, slip_fixtures) -> None:  # type: ignore[no-untyped-def]
    db.commit()
    body = client.get(
        "/v1/proposals", params={"slipped": "true", "slip_bucket": "over_3y", "limit": 100}
    ).json()
    assert {r["public_id"] for r in body["data"]} == {slip_fixtures["ancient"].public_id}


def test_an_empty_slipped_means_no_filter(client, db, slip_fixtures) -> None:  # type: ignore[no-untyped-def]
    """`?slipped=` is an unset form control, not an invalid value."""
    db.commit()
    body = client.get("/v1/proposals", params={"slipped": "", "limit": 100}).json()
    ids = {r["public_id"] for r in body["data"]}
    assert slip_fixtures["ancient"].public_id in ids
    assert slip_fixtures["undated"].public_id in ids


@pytest.mark.parametrize("value", ["", ",,"])
def test_an_empty_slip_bucket_means_no_filter(client, db, slip_fixtures, value) -> None:  # type: ignore[no-untyped-def]
    """It must not fall through to the `slipped=false` arm, which is a filter nobody asked for."""
    db.commit()
    body = client.get("/v1/proposals", params={"slip_bucket": value, "limit": 100}).json()
    ids = {r["public_id"] for r in body["data"]}
    assert slip_fixtures["ancient"].public_id in ids
    assert slip_fixtures["undated"].public_id in ids


def test_an_unrelated_parameter_is_still_a_400(client, db, slip_fixtures) -> None:  # type: ignore[no-untyped-def]
    db.commit()
    assert client.get("/v1/proposals", params={"slippage": "true"}).status_code == 400


def test_the_filter_composes_with_lifecycle_state(client, db, slip_fixtures) -> None:  # type: ignore[no-untyped-def]
    db.commit()
    body = client.get(
        "/v1/proposals", params={"slipped": "true", "lifecycle_state": "contracted", "limit": 100}
    ).json()
    assert {r["public_id"] for r in body["data"]} == {slip_fixtures["ancient"].public_id}


def test_vocabularies_publish_the_bucket_boundaries(client, db) -> None:  # type: ignore[no-untyped-def]
    db.commit()
    buckets = client.get("/v1/meta/vocabularies").json()["data"]["slip_bucket"]
    assert [b["value"] for b in buckets] == list(SLIP_BUCKETS)
    assert buckets[0]["min_days_late"] == SLIP_GRACE_DAYS + 1
    assert buckets[0]["max_days_late"] == 365
    assert buckets[-1]["max_days_late"] is None
    assert all(b["grace_days"] == SLIP_GRACE_DAYS for b in buckets)
