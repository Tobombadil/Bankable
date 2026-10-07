"""Cursor pagination (docs/04-standards.md API-2; api/openapi.yaml `x-filter-grammar`).

Opaque `base64(json({sort key value, tiebreaker id}))`. No offset parameter exists anywhere in
this service.

A cursor is client input, so its payload is checked against the columns it will be bound to
before any SQL is built (backend audit 2026-09-30 F9, F10): `v` must be a scalar of the sort
column's type (an ISO date for a `Date`, an ISO date-time for a `DateTime`, a 64-bit integer for an
integer column, a finite number for a numeric one), and `id` must parse as the tiebreaker column's
type (a UUID for a `GUID`). Anything else is `400 invalid_cursor`, never a driver error. Parsing by
column type also fixes the DATE keyset: a `Date` value used to be turned into a `datetime`, which
SQLite compares as a longer string than the stored date, so pages skipped or repeated rows.
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import json
import math
import uuid as _uuid
from dataclasses import dataclass
from typing import Any, TypeVar

import sqlalchemy as sa
from sqlalchemy import ColumnElement, or_
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.types import TypeEngine

from services.api.errors import invalid_cursor
from services.db.types import GUID

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


#: Postgres `bigint`, and SQLite's integer range: a larger integer cannot be bound at all.
_INT_MIN, _INT_MAX = -(2**63), 2**63 - 1


def _is_scalar(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, int):
        return _INT_MIN <= value <= _INT_MAX
    return value is None or isinstance(value, str)


def decode_cursor(cursor: str, instance: str) -> Cursor:
    """The envelope and the payload's shape: an object with a scalar `v` (string, finite number in
    the 64-bit range, or null) and a string or integer `id`. Column types are checked by
    `coerce_cursor_value` / `coerce_tiebreaker` where the cursor is bound."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except Exception as exc:
        raise invalid_cursor(instance) from exc
    if not isinstance(payload, dict) or set(payload) != {"v", "id"}:
        raise invalid_cursor(instance)
    value, tiebreaker = payload["v"], payload["id"]
    if not _is_scalar(value) or not isinstance(tiebreaker, (str, int)) or isinstance(tiebreaker, bool):
        raise invalid_cursor(instance)
    return Cursor(sort_value=value, tiebreaker_id=str(tiebreaker))


def _python_type(type_: TypeEngine[Any]) -> type | None:
    if isinstance(type_, GUID):
        return _uuid.UUID
    try:
        return type_.python_type
    except NotImplementedError:
        return None


def _parse_datetime(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def coerce_cursor_value(value: Any, type_: TypeEngine[Any], instance: str) -> Any:
    """`v` as the sort column's Python type, or `400 invalid_cursor`. A column whose type does not
    say (an aggregate) takes the scalar as it is; `decode_cursor` has already bounded it."""
    if value is None:
        return None
    python_type = _python_type(type_)
    try:
        if python_type is dt.datetime:
            if not isinstance(value, str):
                raise ValueError(value)
            return _parse_datetime(value)
        if python_type is dt.date:
            if not isinstance(value, str):
                raise ValueError(value)
            return dt.date.fromisoformat(value)
        if python_type is int:
            if not isinstance(value, int):
                raise ValueError(value)
            return value
        if python_type is decimal.Decimal:
            # Encoded as its exact string (`paginate`), so no float round trip moves the boundary.
            number = decimal.Decimal(value if isinstance(value, str) else str(value))
            if not number.is_finite():
                raise ValueError(value)
            return number
        if python_type is float:
            number_f = float(value)
            if not math.isfinite(number_f):
                raise ValueError(value)
            return number_f
        if python_type is _uuid.UUID:
            if not isinstance(value, str):
                raise ValueError(value)
            return str(_uuid.UUID(value))
        if python_type is str and not isinstance(value, str):
            raise ValueError(value)
    except (ValueError, TypeError, OverflowError, decimal.InvalidOperation) as exc:
        raise invalid_cursor(instance) from exc
    return value


def coerce_tiebreaker(value: str, type_: TypeEngine[Any], instance: str) -> Any:
    """`id` as the tiebreaker column's type: a UUID for a `GUID`, a 64-bit integer for an integer
    column, else the string; otherwise `400 invalid_cursor`."""
    python_type = _python_type(type_)
    try:
        if python_type is _uuid.UUID:
            return str(_uuid.UUID(value))
        if python_type is int:
            number = int(value)
            if not _INT_MIN <= number <= _INT_MAX:
                raise ValueError(value)
            return number
    except (ValueError, TypeError) as exc:
        raise invalid_cursor(instance) from exc
    return value


def _is_not_null(column: InstrumentedAttribute[Any]) -> bool:
    """True for a mapped table column declared NOT NULL; anything else (an aggregate, a label, a
    nullable column) keeps the NULL-aware keyset."""
    expression = getattr(column, "expression", None)
    return isinstance(expression, sa.Column) and not expression.nullable


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

    A sort column declared NOT NULL is ordered and resumed without the NULL handling a nullable
    one needs: no `NULLS LAST` (it changes nothing there, and it stops Postgres matching the
    column's index, which sorts `DESC` with NULLs first) and a row-value keyset
    `(sort, id) > (v, t)` instead of an `OR` with a dead `IS NULL` arm, so an index on
    `(sort, id)` can start the page at the cursor (backend audit 2026-10-07, PERF-3; migration
    0035). The rows and their order are the same either way.
    """
    not_null = _is_not_null(sort_column)
    if cursor:
        decoded = decode_cursor(cursor, instance)
        value = coerce_cursor_value(decoded.sort_value, sort_column.type, instance)
        tiebreaker = coerce_tiebreaker(decoded.tiebreaker_id, id_column.type, instance)
        # NULL sort values sort last in both directions (explicit `NULLS LAST`, so SQLite and
        # Postgres agree; API audit 2026-09-18, S5: a cursor whose sort value was NULL raised
        # ArgumentError and 500ed, and `due_at`, the default opportunity sort, is nullable).
        where: ColumnElement[bool]
        if value is None:
            after_id = id_column > tiebreaker if ascending else id_column < tiebreaker
            where = sa.and_(sort_column.is_(None), after_id)
        elif not_null:
            key = sa.tuple_(sort_column, id_column)
            bound = sa.tuple_(
                sa.literal(value, type_=sort_column.type), sa.literal(tiebreaker, type_=id_column.type)
            )
            where = key > bound if ascending else key < bound
        elif ascending:
            where = or_(
                sort_column > value,
                sort_column.is_(None),
                sa.and_(sort_column == value, id_column > tiebreaker),
            )
        else:
            where = or_(
                sort_column < value,
                sort_column.is_(None),
                sa.and_(sort_column == value, id_column < tiebreaker),
            )
        stmt = stmt.where(where)

    order = sort_column.asc() if ascending else sort_column.desc()
    if not not_null:
        order = order.nulls_last()
    tiebreak = id_column.asc() if ascending else id_column.desc()
    stmt = stmt.order_by(order, tiebreak).limit(limit + 1)

    rows = list(session.scalars(stmt).all())
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        last_sort_value = getattr(last, sort_column.key)
        if isinstance(last_sort_value, (dt.datetime, dt.date)):
            last_sort_value = last_sort_value.isoformat()
        elif isinstance(last_sort_value, decimal.Decimal):
            last_sort_value = str(last_sort_value)
        next_cursor = encode_cursor(last_sort_value, str(getattr(last, id_column.key)))
    return page_rows, next_cursor, has_more
