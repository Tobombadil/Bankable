"""Process-local cache for the proposal map's whole-set work, keyed on a data version (backend audit
2026-10-07, PERF-2).

`GET /v1/proposals/geo` reads every visible proposal matching its filters on every call, whatever
the viewport, because its totals and licence summary are national by design (docs/04 D-8). A pan
changes only `bbox`/`zoom`, so the whole-set part (`records._proposal_geo_features` and the licence
aggregate) is the same answer until the data changes. This module decides when that is, the way
`services/api/assets.py` already does for the asset layer, and holds the answers.

A cached answer is reused only while all of these are unchanged:

* the database (an engine token, never `id()`, which a new engine can reuse after garbage
  collection);
* the request's filters (every query parameter except `bbox` and `zoom`) and the caller's tier;
* the UTC date (the slippage filters compare against today);
* a **data version** read in one statement: proposal count and `max(updated_at)` (any ORM write to a
  proposal moves it, including survivorship, merges, overrides and takedowns), proposal-link count,
  source `max(updated_at)` and `max(last_loaded_ts)` (every committed load moves the latter; a
  publish-state flip moves the former), licence `max(updated_at)`; plus organisation count and
  `max(updated_at)` when `sponsor_id` or `q` reads organisations;
* this process's **write generation**: any INSERT, UPDATE, DELETE, TRUNCATE or DDL statement this
  process sends that names a table the map reads bumps it, when sent and again at commit (so a
  reader whose snapshot predates the commit cannot store an old answer under the new generation).
  This is what catches an in-process write the data version cannot see (a link deactivated, a
  location moved);
* and the entry is younger than `TTL_SECONDS`, the backstop for a write from another process
  that the data version does not see. Public responses are edge-cached for 300 s already
  (docs/23 §1), so 60 s adds no staleness a reader could not already get.

Entries are bounded (`MAX_ENTRIES`, least recently used out). The cached value holds plain values
only; rows a hidden source gates are held by id and re-read through `GatedRecord` on every request,
so no ORM object outlives its session.
"""

from __future__ import annotations

import datetime as dt
import itertools
import re
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import Any

import sqlalchemy as sa
from fastapi import Request
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from services.db.models import Licence, Location, Organization, Proposal, ProposalSource, Source

TTL_SECONDS = 60.0
MAX_ENTRIES = 16

#: The tables whose rows can change a cached answer (the proposal map; the coverage statement). A
#: write naming any of them bumps the generation.
WATCHED_TABLES = frozenset(
    {
        "proposal",
        "proposal_source",
        "location",
        "source",
        "licence",
        "organization",
        "organization_alias",
        "interconnection_point",
        # Read by the coverage statement (`services/api/coverage.py`), cached the same way.
        "opportunity",
        "opportunity_source",
        "asset",
        "asset_owner",
        "asset_source",
    }
)
_WRITE_VERB = re.compile(r"^\s*(insert|update|delete|replace|truncate|drop|create|alter|copy|merge)\b", re.I)
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_lock = threading.Lock()
_generation = 0
_engine_tokens: weakref.WeakKeyDictionary[Engine, int] = weakref.WeakKeyDictionary()
_token_counter = itertools.count(1)


def _names_watched_table(statement: str) -> bool:
    if not _WRITE_VERB.match(statement):
        return False
    return any(word.lower() in WATCHED_TABLES for word in _WORD.findall(statement))


def _bump() -> None:
    global _generation
    with _lock:
        _generation += 1


def generation() -> int:
    with _lock:
        return _generation


@event.listens_for(Engine, "before_cursor_execute")
def _on_execute(
    conn: sa.Connection, _cursor: Any, statement: str, _params: Any, _context: Any, _many: bool
) -> None:
    if _names_watched_table(statement):
        _bump()
        conn.info["geo_cache_dirty"] = True


@event.listens_for(Engine, "commit")
def _on_commit(conn: sa.Connection) -> None:
    if conn.info.pop("geo_cache_dirty", False):
        _bump()


@event.listens_for(Engine, "rollback")
def _on_rollback(conn: sa.Connection) -> None:
    conn.info.pop("geo_cache_dirty", None)


def engine_token(db: Session) -> int:
    bind = db.get_bind()
    engine = bind.engine if isinstance(bind, sa.Connection) else bind
    with _lock:
        token = _engine_tokens.get(engine)
        if token is None:
            token = _engine_tokens[engine] = next(_token_counter)
        return token


def version_terms(*, organizations: bool = False) -> list[Any]:
    """The scalar subqueries `data_version` reads (module docstring), for a caller that adds its
    own to the same statement (the coverage statement's version, `services/api/coverage.py`)."""
    terms: list[Any] = [
        select_scalar(sa.func.count(), Proposal),
        select_scalar(sa.func.max(Proposal.updated_at), Proposal),
        select_scalar(sa.func.count(), ProposalSource),
        select_scalar(sa.func.max(Source.updated_at), Source),
        select_scalar(sa.func.max(Source.last_loaded_ts), Source),
        select_scalar(sa.func.max(Licence.updated_at), Licence),
        select_scalar(sa.func.count(), Location),
    ]
    if organizations:
        terms += [
            select_scalar(sa.func.count(), Organization),
            select_scalar(sa.func.max(Organization.updated_at), Organization),
        ]
    return terms


def run_version(db: Session, terms: list[Any]) -> tuple[Any, ...]:
    row = db.execute(sa.select(*terms)).one()
    return tuple(v.isoformat() if isinstance(v, dt.datetime) else v for v in row)


def data_version(db: Session, *, organizations: bool = False) -> tuple[Any, ...]:
    """The aggregates named in the module docstring, in one statement. Every term is an index
    lookup or a count over an index (`ix_proposal_updated_at`, migration 0035)."""
    return run_version(db, version_terms(organizations=organizations))


def select_scalar(expr: Any, model: Any) -> Any:
    return sa.select(expr).select_from(model).scalar_subquery()


def filter_key(request: Request, ignore: Iterable[str] = ("bbox", "zoom")) -> tuple[tuple[str, str], ...]:
    skip = set(ignore)
    return tuple(sorted((k, v) for k, v in request.query_params.multi_items() if k not in skip))


class TtlLru:
    """A small thread-safe LRU whose entries expire `ttl` seconds after they were stored."""

    def __init__(self, *, max_entries: int = MAX_ENTRIES, ttl: float = TTL_SECONDS) -> None:
        self.max_entries = max_entries
        self.ttl = ttl
        self._items: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.clock: Callable[[], float] = time.monotonic

    def get(self, key: Any) -> Any | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            stored_at, value = item
            if self.clock() - stored_at > self.ttl:
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return value

    def put(self, key: Any, value: Any) -> None:
        with self._lock:
            self._items[key] = (self.clock(), value)
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def cache_key(db: Session, request: Request, entitlement: str, *, today: dt.date) -> tuple[Any, ...]:
    """Everything a proposal map answer depends on except the viewport. `today` is the date the
    slippage filters compare against (the caller's own clock, `records.slip_today`). The generation
    is read before the data version so a write landing between the two can only make the key older
    than the data it describes, never newer."""
    gen = generation()
    qp = request.query_params
    organizations = bool(qp.get("sponsor_id") or qp.get("q"))
    return (
        engine_token(db),
        gen,
        entitlement,
        today.isoformat(),
        filter_key(request),
        data_version(db, organizations=organizations),
    )


#: `(engine token, generation, data version)` -> the hidden source ids that supply no stored proposal
#: field, location or link (`unused_hidden_sources`).
_unused_hidden: TtlLru = TtlLru(max_entries=8)


def unused_hidden_sources(db: Session, hidden: frozenset[str]) -> frozenset[str]:
    """The ids in `hidden` that no proposal can be served from: no proposal link names them, no
    location was placed by them, and no proposal's `field_provenance` mentions them (backend audit
    2026-10-07, PERF-2a). Dropping such a source from the map's hidden set changes nothing the map
    serves, because every hidden-source rule it applies (the field gate, the placement's own source)
    reads one of those three; and it switches the per-row JSON clauses off when the only hidden
    sources are registry-only ones (an admin-registered source starts `ingest_only`). Computed once
    per data version and shared by every filter set."""
    if not hidden:
        return frozenset()
    key = (engine_token(db), generation(), data_version(db))
    cached = _unused_hidden.get(key)
    if cached is not None and cached[0] == hidden:
        known: frozenset[str] = cached[1]
        return known
    unused = set()
    for sid in sorted(hidden):
        used = db.execute(
            sa.select(
                sa.or_(
                    sa.exists().where(ProposalSource.source_id == sid),
                    sa.exists().where(Location.source_id == sid),
                    sa.exists().where(
                        sa.cast(Proposal.field_provenance, sa.Text).contains(f'"{sid}"', autoescape=True)
                    ),
                )
            )
        ).scalar()
        if not used:
            unused.add(sid)
    result = frozenset(unused)
    _unused_hidden.put(key, (hidden, result))
    return result


def reset() -> None:
    """Test hook: forget every cached answer (the generation keeps counting)."""
    _unused_hidden.clear()
    for cache in list(_registered):
        cache.clear()


_registered: list[TtlLru] = []


def register(cache: TtlLru) -> TtlLru:
    _registered.append(cache)
    return cache


__all__ = [
    "MAX_ENTRIES",
    "TTL_SECONDS",
    "WATCHED_TABLES",
    "TtlLru",
    "cache_key",
    "data_version",
    "engine_token",
    "filter_key",
    "generation",
    "register",
    "reset",
    "unused_hidden_sources",
]
