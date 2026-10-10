"""`services/api/listing.py`: whether any register still lists a proposal (lane P, 2026-10-10)."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.conftest import make_open_licence, make_public_source, make_visible_proposal
from services.api.errors import ProblemError
from services.api.listing import listed_clause, listed_param, listing_state
from services.db.models import Proposal

UTC = dt.UTC
EARLY = dt.datetime(2026, 8, 1, tzinfo=UTC)
LATE = dt.datetime(2026, 9, 15, tzinfo=UTC)


def _links(*gone: dt.datetime | None) -> list[Any]:
    return [SimpleNamespace(gone_at=g) for g in gone]


def test_one_current_link_keeps_a_record_listed() -> None:
    assert listing_state(_links(EARLY, None)) == (True, None)


def test_a_record_every_register_dropped_is_dated_by_the_last_to_drop_it() -> None:
    assert listing_state(_links(EARLY, LATE)) == (False, LATE)


def test_a_record_with_no_visible_link_is_not_listed_and_undated() -> None:
    assert listing_state([]) == (False, None)


def test_the_param_reads_true_false_or_nothing_and_refuses_the_rest() -> None:
    assert listed_param(None, "/v1/proposals") is None
    assert listed_param("", "/v1/proposals") is None
    assert listed_param("true", "/v1/proposals") is True
    assert listed_param("false", "/v1/proposals") is False
    with pytest.raises(ProblemError):
        listed_param("TRUE", "/v1/proposals")  # spelt as `slipped` is


def test_the_sql_twin_agrees_with_the_python_one(db: Session) -> None:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    current = make_visible_proposal(db, src, public_id_suffix="1")
    gone = make_visible_proposal(db, src, public_id_suffix="2")
    gone.sources[0].gone_at = LATE
    db.commit()
    listed = set(db.scalars(select(Proposal.public_id).where(listed_clause("public"))))
    assert listed == {current.public_id}
    for prop in (current, gone):
        assert listing_state(prop.sources)[0] is (prop.public_id in listed)
