"""Cursor pagination (docs/04-standards.md API-2; api/openapi.yaml `x-filter-grammar`).

Opaque `base64(json({sort key value, tiebreaker id}))`. No offset parameter exists anywhere in
this service.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
from dataclasses import dataclass
from typing import Any, TypeVar

import sqlalchemy as sa
from sqlalchemy import ColumnElement, or_
from sqlalchemy.orm import InstrumentedAttribute, Session

from services.api.errors import invalid_cursor

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

T = TypeVar("T")


@dataclass
class Cursor:
    sort_value: Any
    tiebreaker_id: str


def encode_cursor(sort_value: Any, tiebreaker_id: str) -> str:
    payload = json.dumps({"v": sort_value, "id": tiebreaker_id}, default=str)
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, instance: str) -> Cursor:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        return Cursor(sort_value=payload["v"], tiebreaker_id=str(payload["id"]))
    except Exception as exc:
        raise invalid_cursor(instance) from exc


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))


def paginate(
    session: Session,
    stmt: sa.Select[tuple[T]],
    *,
    sort_column: InstrumentedAttribute[Any],
    id_column: InstrumentedAttribute[Any],
    ascending: bool,
    cursor: str | None,
    limit: int,
    instance: str,
) -> tuple[list[T], str | None, bool]:
    """Generic keyset pagination (docs/04 API-2). Only forward pagination is implemented this
    sprint — `page.prev_cursor` is always null; reverse iteration is an open decision in
    services/README.md, not a silent gap (the field is present and honestly null, never wrong).
    """
    if cursor:
        decoded = decode_cursor(cursor, instance)
        value, tiebreaker = decoded.sort_value, decoded.tiebreaker_id
        if isinstance(value, str):
            try:
                value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass  # a non-datetime sort field (name, score, seq as string) — leave as-is
        if ascending:
            where: ColumnElement[bool] = or_(
                sort_column > value, sa.and_(sort_column == value, id_column > tiebreaker)
            )
        else:
            where = or_(sort_column < value, sa.and_(sort_column == value, id_column < tiebreaker))
        stmt = stmt.where(where)

    order = sort_column.asc() if ascending else sort_column.desc()
    tiebreak = id_column.asc() if ascending else id_column.desc()
    stmt = stmt.order_by(order, tiebreak).limit(limit + 1)

    rows = list(session.scalars(stmt).all())
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        last_sort_value = getattr(last, sort_column.key)
        if isinstance(last_sort_value, dt.datetime):
            last_sort_value = last_sort_value.isoformat()
        next_cursor = encode_cursor(last_sort_value, str(getattr(last, id_column.key)))
    return page_rows, next_cursor, has_more
