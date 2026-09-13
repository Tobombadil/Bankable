"""Admin `/admin/v1` operations for source health, the licence gate, cost reporting and the audit
log (Sprint 3 item 3; docs/10-prd-mvp.md §4.9 US-901/US-903/US-904/US-905/US-909; docs/20 §8;
docs/21 §4.1-§4.3, §8). Mounted by the coordinator with one `include_router(router)` call; this
module never imports or edits `services/api/app.py`.

Every operation below is `x-status: planned` in `api/openapi.yaml`; the coordinator flips the
status once this module is wired in. Implemented exactly to the spec's request/response schemas,
with the deviations recorded here (also in `services/api/admin_sources.md`):

Decisions
---------
D1. **Every write requires a non-blank `reason`, even where the spec's own request schema does not
    mark it `required`** (`AdminSourceRunRequest`). The task brief's hard rule ("every write body
    carries `reason`; 400 `validation_error` if missing/blank") is stricter than the spec here and
    is followed; the spec's own admin surfaces (US-905 AC3, US-901 AC2, docs/04 S-4) already assume
    every admin action is audited with a reason, so this is a tightening, not a contradiction.
D2. **`Source.id` and `Licence.id` are text primary keys, but `event.subject_id` is a `uuid`
    column** (`services/db/models.py` `Event.subject_id: Mapped[uuid.UUID]`, no FK). Audit events on
    a source or licence use a deterministic `uuid5` derived from the text id
    (`_source_subject_uuid` / `_licence_subject_uuid`) so the same source/licence always audits to
    the same synthetic subject id; the real identifier is always also present in `before`/`after`.
D3. **`api/openapi.yaml`'s `SubjectType` enum has no `licence` value**, yet `Licence` is a distinct
    entity from `Source` (one licence can cover many sources) and US-905's gate lives on the
    licence, not the source. Audit rows for `PUT .../licences/{id}/gate` are written with
    `subject_type = "licence"` (accurate) rather than misattributing them to `subject_type =
    "source"` (inaccurate — a gate change is not scoped to one source). This is a genuine, narrow
    spec gap (SubjectType should grow a `licence` value) and is flagged for the architect rather
    than silently worked around.
D4. **`AnyPublicIdValue`'s pattern (`^(prop|opp|org|mat|evt|doc)_...$`) does not cover `source`,
    `licence`, `account`, `user` or `api_key` subjects**, even though `SubjectType`'s enum lists all
    of those as valid subject types for an `Event`. Consequently `GET /admin/v1/audit` rows whose
    `subject_type` is `source` or `licence` cannot validate `subject_id` against `Event`'s schema at
    all — no rendering choice fixes this, since every candidate string fails the fixed prefix list.
    `admin_sources.md` documents this precisely; `tests/test_api_admin_sources.py` validates the
    envelope/list shape and one fully spec-compliant row (`subject_type = "match"`, which *is*
    covered) with `assert_valid`, and separately asserts the content of source/licence audit rows
    (reason, before/after, filtering) without full JSON-Schema validation on those specific rows.
D5. **A licence reclassification (`reuse_class` changes) creates a new `Licence` row** (invariant
    L2, per the spec's own description) rather than mutating `reuse_class` in place, so that
    historical `snapshot`/`proposal_source`/`opportunity_source` rows keep the licence that applied
    at fetch time. Every `Source` currently pointing at the old licence id is repointed to the new
    one (future fetches and gate checks use the new classification); the new id is
    `{old_id}-{reuse_class}`, de-duplicated with a numeric suffix on collision.
D6. **No idempotency-key replay is implemented** for the `Idempotency-Key` header on any operation
    here, matching every other write endpoint in this codebase today (`services/api/pro.py` accepts
    the same header on `api/openapi.yaml` operations without implementing replay either) — recorded
    as an existing, pre-sprint gap rather than one introduced here.
D7. **No `relag` job is enqueued** when `PATCH .../sources/{id}` changes `lag_days`/`lag_overrides`
    (docs/21 §5.4 describes one recomputing `public_at` on historical rows asynchronously); no such
    job exists anywhere in this repo yet (`infra/scheduler` only defines `run_connector`/tick
    tasks). The runtime columns are updated immediately; historical `public_at` recomputation is
    deferred to whichever sprint adds the job, and is out of this module's read/write scope.
D8. **`AdminSource.host`/`schedule_cron`** are required, non-nullable strings in the spec, but
    `Source.host`/`schedule_cron` are nullable manifest/runtime columns that the loader (out of
    scope here) may not yet have populated for every row. Serialization falls back to `""` rather
    than crashing or inventing a value — this module never computes a cadence-derived cron or a
    URL-derived host outside of source *creation* (`admin_create_source`, where the host is real
    and needed for the egress allowlist per the spec's own description).
D9. **`GET /admin/v1/costs`**: `CostRow.day` is required and non-nullable, so even a `group_by` that
    omits `day` still returns one row per day (day cannot be suppressed) — only `source_id`,
    `purpose` and `model_alias` are actually elided by `group_by`. Aggregation is done in Python
    over the matching `model_call` and `source_run` rows (not SQL `date_trunc`) to stay portable
    across the Postgres target and this sprint's SQLite test database, matching this codebase's
    existing practice of avoiding dialect-specific date functions (`services/api/common.py`
    `ensure_aware`'s docstring makes the same SQLite-portability trade-off). `flagged_sources` reads
    the existing rolling `source.cost_per_changed_record_30d` column against the threshold — it is
    not recomputed from the windowed rows, since it is documented as a separately-maintained
    30-day metric (docs/21 §4.1), optionally narrowed by the `source_id` filter.
D10. **`GateUnmet`'s `Problem.gate`/`Problem.missing` fields are not emitted** — `ProblemError`
     (`services/api/errors.py`, read-only for this module) has no keyword for them. Both fields are
     optional in the `Problem` schema, so omitting them is schema-valid; the unmet condition(s) are
     named in `detail` instead, satisfying US-905 AC1's "refused ... naming the unmet condition".
D11. Curated issuers (`POST /admin/v1/sources`) are created with `tier = 3`, `format = null`,
     `schedule_cron = null` (D8 explains why a null/empty cron is acceptable) and `egress = "plain"`
     — the last one is not a guess: the spec's own description says "The issuer URL host is added
     to the egress allowlist for the `plain` pool only", which only makes sense if the row's egress
     class is `plain`.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid as _uuid
from typing import Annotated, Any, Protocol
from urllib.parse import urlparse

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.audit import record_audit_event
from services.api.auth import AuthContext, require_admin
from services.api.common import utcnow
from services.api.deps import get_db
from services.api.errors import ProblemError, not_found, validation_error
from services.api.pagination import clamp_limit, paginate
from services.api.params import check_allowed, csv_param
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
    serialize_licence_embedded,
    serialize_source,
)
from services.db.models import (
    REUSE_CLASSES,
    SOURCE_PUBLISH_STATES,
    Event,
    Licence,
    ModelCall,
    Organization,
    Snapshot,
    Source,
    SourceRun,
    User,
)
from services.ids import _CROCKFORD, public_id

router = APIRouter()

# ------------------------------------------------------------------------------------ constants
#: Default flag threshold for `cost_per_changed_record_30d` (docs/21 §4.1 `[A-9]`).
DEFAULT_COST_THRESHOLD_USD = 0.50
#: US-303 AC1 / AdminSourceCreate: curated issuers may only be added via these access methods.
_CURATED_ACCESS_VALUES = ("html", "rss", "api")
_SOURCE_ID_PATTERN = re.compile(r"^[a-z]{2,6}(\.[a-z0-9_]+){1,4}$")
_GROUP_BY_FIELDS = ("purpose", "source_id", "day", "model_alias")
_COST_PURPOSES = ("extract", "adjudicate", "draft", "classify", "other")


def _require_reason(body: dict[str, Any] | None, path: str) -> str:
    reason = (body or {}).get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise validation_error("reason", "reason is required and may not be blank", path)
    return reason


def _parse_dt(value: Any, field: str, path: str) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
        except ValueError:
            pass
    raise validation_error(field, "must be an RFC 3339 date-time", path)


def _parse_date(value: str, field: str, path: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise validation_error(field, "must be an ISO date (YYYY-MM-DD)", path) from exc


# ------------------------------------------------------------------------- synthetic subject ids
# See module docstring D2: `Source`/`Licence` primary keys are text, `event.subject_id` is a uuid.
def _source_subject_uuid(source_id: str) -> _uuid.UUID:
    return _uuid.uuid5(_uuid.NAMESPACE_URL, f"bankable:source:{source_id}")


def _licence_subject_uuid(licence_id: str) -> _uuid.UUID:
    return _uuid.uuid5(_uuid.NAMESPACE_URL, f"bankable:licence:{licence_id}")


# --------------------------------------------------------------------- run_/snap_ public ids (D2)
# `SourceRun`/`Snapshot` have no `public_id` column (docs/21 §4.2/§4.3); ids are synthesised from
# the row's real uuid with `services.ids.public_id` and decoded the same way
# `services/crm/router.py`'s `_find_match_by_public_id` decodes `mat_` ids.
def _decode_crockford_uuid(value: str, prefix: str) -> _uuid.UUID | None:
    if not value.startswith(f"{prefix}_"):
        return None
    digits = value[len(prefix) + 1 :]
    try:
        n = 0
        for ch in digits:
            n = n * 32 + _CROCKFORD.index(ch)
        return _uuid.UUID(int=n)
    except (ValueError, OverflowError):
        return None


def _find_source_run(db: Session, run_id: str) -> SourceRun | None:
    candidate = _decode_crockford_uuid(run_id, "run")
    return db.get(SourceRun, candidate) if candidate is not None else None


def _find_snapshot(db: Session, snapshot_id: str) -> Snapshot | None:
    candidate = _decode_crockford_uuid(snapshot_id, "snap")
    return db.get(Snapshot, candidate) if candidate is not None else None


# ---------------------------------------------------------------------------- "run now" (US-904)
class SourceRunner(Protocol):
    """Enqueues a fetch for `source` and returns the `source_run` row the panel shows immediately.
    The worker (a later sprint wave) updates that row when it actually runs the connector — this
    endpoint never runs a connector inline."""

    def enqueue(self, db: Session, source: Source, *, trigger: str, requested_by: User) -> SourceRun: ...


class QueuedSourceRunner:
    """Production implementation: creates the `source_run` row, then defers `run_connector`
    (`infra/scheduler/app.py`) onto the correct queue with the same per-source `queueing_lock`
    the periodic tick uses (`infra/scheduler/cadence.py` `queueing_lock_for`) so a manual run and a
    scheduled tick cannot double-enqueue the same source. Every failure to reach the queue — no
    Postgres, `procrastinate` import failure, `AlreadyEnqueued` — is wrapped into a fixed `503
    unavailable` rather than leaking a stack trace or a raw driver exception; `services/api/deps.py`
    `get_db` then rolls back the whole request, so the `source_run` row created below never
    persists when the enqueue itself fails."""

    def enqueue(self, db: Session, source: Source, *, trigger: str, requested_by: User) -> SourceRun:
        now = utcnow()
        run = SourceRun(
            source_id=source.id,
            trigger=trigger,
            started_at=now,
            status="running",
            egress_class=source.egress,
        )
        db.add(run)
        db.flush()
        try:
            from infra.scheduler.app import run_connector
            from infra.scheduler.cadence import queue_for_source, queueing_lock_for

            queue = queue_for_source({"egress": source.egress, "access": source.access})
            run_connector.configure(queue=queue, queueing_lock=queueing_lock_for(source.id)).defer(
                source_id=source.id
            )
        except Exception as exc:  # pragma: no cover - exercised via FakeSourceRunner in tests
            raise ProblemError(
                "unavailable",
                "Run queue unavailable",
                detail="The connector run queue could not be reached; try again shortly.",
            ) from exc
        return run


def get_source_runner() -> SourceRunner:
    return QueuedSourceRunner()


# ------------------------------------------------------------------------------- serializers
def _serialize_admin_source(source: Source) -> dict[str, Any]:
    licence = source.licence
    threshold = DEFAULT_COST_THRESHOLD_USD
    cost_flagged = (
        source.cost_per_changed_record_30d is not None
        and float(source.cost_per_changed_record_30d) > threshold
    )
    last_run = source.__dict__.get("_admin_last_run")  # set by callers that pre-fetch it
    out = serialize_source(source)
    out.update(
        {
            "effort": source.effort,
            "egress": source.egress,
            "connector": source.connector,
            "schedule_cron": source.schedule_cron or "",  # D8
            "next_run_at": _iso_or_none(source.next_run_at),
            "paused": source.paused,
            "health": source.health,
            "consecutive_failures": source.consecutive_failures,
            "last_error": source.last_error,
            "last_error_at": _iso_or_none(source.last_error_at),
            "host": source.host or "",  # D8
            "max_rps": float(source.max_rps),
            "max_concurrency": source.max_concurrency,
            "enrichment_enabled": source.enrichment_enabled,
            "model_budget_usd_daily": (
                float(source.model_budget_usd_daily) if source.model_budget_usd_daily is not None else None
            ),
            "cost_per_changed_record_30d": (
                float(source.cost_per_changed_record_30d)
                if source.cost_per_changed_record_30d is not None
                else None
            ),
            "cost_flagged": cost_flagged,
            "last_run": _serialize_source_run(last_run) if last_run is not None else None,
            "licence_detail": _serialize_admin_licence(licence, source_count=None),
            "legal_evidence_url": licence.evidence_url,
        }
    )
    return out


def _iso_or_none(value: dt.datetime | None) -> str | None:
    from services.api.common import iso

    return iso(value)


def _serialize_source_run(run: SourceRun) -> dict[str, Any]:
    return {
        "run_id": public_id("run", run.id),
        "source_id": run.source_id,
        "trigger": run.trigger,
        "started_at": _iso_or_none(run.started_at),
        "finished_at": _iso_or_none(run.finished_at),
        "status": run.status,
        "snapshot_id": None,  # a running/just-enqueued run has no snapshot yet; the worker sets one
        "http_status": run.http_status,
        "bytes": run.bytes,
        "egress_class": run.egress_class,
        "rows_seen": run.rows_seen,
        "rows_new": run.rows_new,
        "rows_changed": run.rows_changed,
        "rows_gone": run.rows_gone,
        "events_emitted": run.events_emitted,
        "model_calls": run.model_calls,
        "cost_usd": float(run.cost_usd),
        "worker_seconds": float(run.worker_seconds),
        "dq_status": run.dq_status or "pass",
        "dq": run.dq or {},
        "error": run.error,
        "error_class": run.error_class,
        "attempt": run.attempt,
        "dead_lettered": run.dead_lettered,
    }


def _serialize_snapshot(snap: Snapshot) -> dict[str, Any]:
    return {
        "snapshot_id": public_id("snap", snap.id),
        "source_id": snap.source_id,
        "run_id": public_id("run", snap.source_run_id),
        "object_key": snap.object_key,
        "sha256": snap.sha256,
        "byte_size": snap.byte_size,
        "content_type": snap.content_type,
        "fetched_url": snap.fetched_url,
        "http_status": snap.http_status,
        "retrieved_at": _iso_or_none(snap.retrieved_at),
        "licence_id": snap.licence_id,
        "parser_version": snap.parser_version,
        "record_count": snap.record_count,
        "previous_snapshot_id": (
            public_id("snap", snap.previous_snapshot_id) if snap.previous_snapshot_id else None
        ),
        "retention_class": snap.retention_class,
        "expires_at": _iso_or_none(snap.expires_at),
        "download_url": None,  # object storage pre-signing is not wired this sprint
    }


def _serialize_admin_licence(licence: Licence, *, source_count: int | None) -> dict[str, Any]:
    out = serialize_licence_embedded(licence)
    out.update(
        {
            "gate_cleared_at": _iso_or_none(licence.gate_cleared_at),
            "expires_at": _iso_or_none(licence.expires_at),
            "gate_cleared_by": None,  # gate_cleared_by is a user uuid; no public-id lookup here
            "evidence_url": licence.evidence_url,
            "evidence_retrieved_at": _iso_or_none(licence.evidence_retrieved_at),
            "evidence_object_key": licence.evidence_object_key,
            "classified_by": licence.classified_by,
            "contract_ref": licence.contract_ref,
            "notes": licence.notes,
        }
    )
    if source_count is not None:
        out["source_count"] = source_count
    return out


def _serialize_admin_audit_event(event: Event) -> dict[str, Any]:
    """Event shape for `GET /admin/v1/audit` (docs/20 §8 item 8: "the `event` table filtered to
    `actor_type = user`"). See module docstring D3/D4: `subject_id` is rendered as a best-effort
    public id and will not satisfy `AnyPublicIdValue`'s pattern for `source`/`licence` subjects —
    a spec gap, not a bug here. `subject`/`provenance` are optional on `Event` and are omitted
    rather than guessed at for subject types this module does not own a name/url renderer for."""
    return {
        "id": public_id("evt", event.id),
        "seq": event.seq,
        "subject_type": event.subject_type,
        "subject_id": _best_effort_subject_public_id(event),
        "event_type": event.event_type,
        "headline": f"{event.subject_type} {event.event_type}",
        "observed_at": _iso_or_none(event.observed_at) or "",
        "recorded_at": _iso_or_none(event.recorded_at) or "",
        "published_at": _iso_or_none(event.published_at),
        "public_at": _iso_or_none(event.public_at),
        "before": event.before,
        "after": event.after,
        "changed_keys": event.changed_keys or [],
        "actor_type": event.actor_type,
        "actor_user_id": None,
        "reason": event.reason,
        "confidence": float(event.confidence) if event.confidence is not None else None,
        "reverses_event_id": None,
        "provenance": None,
    }


def _best_effort_subject_public_id(event: Event) -> str:
    """Real business identifier when one is carried in `after`/`before` (D4); falls back to the
    synthetic subject uuid rendered with the standard Crockford scheme so the field is always a
    string even though it may not satisfy `AnyPublicIdValue`'s fixed prefix list."""
    payload = event.after or event.before or {}
    for key in ("source_id", "licence_id"):
        if isinstance(payload.get(key), str):
            return str(payload[key])
    return public_id(event.subject_type[:3], event.subject_id)


# =============================================================================== GET /sources
@router.get("/admin/v1/sources")
def admin_list_sources(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    check_allowed(
        request, {"limit", "cursor", "category", "health", "publish_state", "reuse_class", "implemented"}
    )
    limit = clamp_limit(_int_param(request, "limit"))
    qp = request.query_params
    stmt = select(Source)
    if v := qp.get("category"):
        stmt = stmt.where(Source.category.in_(csv_param(v)))
    if v := qp.get("health"):
        stmt = stmt.where(Source.health.in_(csv_param(v)))
    if v := qp.get("publish_state"):
        stmt = stmt.where(Source.publish_state.in_(csv_param(v)))
    if v := qp.get("implemented"):
        stmt = stmt.where(Source.implemented == (v.lower() == "true"))
    if v := qp.get("reuse_class"):
        stmt = stmt.join(Licence, Source.licence_id == Licence.id).where(
            Licence.reuse_class.in_(csv_param(v))
        )
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=Source.id,
        id_column=Source.id,
        ascending=True,
        cursor=qp.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    last_runs = _last_runs_for(db, [r.id for r in rows])
    for row in rows:
        row.__dict__["_admin_last_run"] = last_runs.get(row.id)
    data = [_serialize_admin_source(r) for r in rows]
    return build_list_envelope(
        data,
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
        page=build_page(next_cursor, None, has_more),
    )


def _int_param(request: Request, name: str) -> int | None:
    v = request.query_params.get(name)
    return int(v) if v is not None else None


def _last_runs_for(db: Session, source_ids: list[str]) -> dict[str, SourceRun]:
    if not source_ids:
        return {}
    rows = list(
        db.scalars(
            select(SourceRun)
            .where(SourceRun.source_id.in_(source_ids))
            .order_by(SourceRun.source_id, SourceRun.started_at.desc())
        ).all()
    )
    latest: dict[str, SourceRun] = {}
    for r in rows:
        latest.setdefault(r.source_id, r)
    return latest


# =============================================================================== POST /sources
@router.post("/admin/v1/sources", status_code=201)
def admin_create_source(
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    path = request.url.path
    reason = _require_reason(body, path)
    source_id = body.get("source_id")
    name = body.get("name")
    operator = body.get("operator")
    url = body.get("url")
    access = body.get("access")
    cadence = body.get("cadence")
    licence_id = body.get("licence_id")
    if not isinstance(source_id, str) or not _SOURCE_ID_PATTERN.match(source_id):
        raise validation_error("source_id", "must match the sources.yaml id pattern", path)
    if not name or not operator or not url or not cadence:
        raise validation_error("name", "name, operator, url and cadence are required", path)
    if access not in _CURATED_ACCESS_VALUES:
        raise validation_error("access", f"must be one of {_CURATED_ACCESS_VALUES}", path)
    if not isinstance(licence_id, str):
        raise validation_error("licence_id", "licence_id is required", path)
    if db.get(Source, source_id) is not None:
        raise ProblemError(
            "conflict", "Source already exists", detail=f"{source_id!r} is already registered."
        )
    licence = db.get(Licence, licence_id)
    if licence is None:
        raise not_found(path, detail=f"No licence {licence_id!r} exists.")

    issuer_org_id = body.get("issuer_org_id")
    if issuer_org_id is not None:
        org = db.scalar(select(Organization).where(Organization.public_id == issuer_org_id))
        if org is None:
            raise validation_error("issuer_org_id", "no such organization", path)
        org.is_curated_issuer = True

    host = urlparse(url).netloc.lower() or None
    source = Source(
        id=source_id,
        name=name,
        jurisdiction=body.get("jurisdiction"),
        category="procurement",  # US-303 AC1; AdminSourceCreate.category is a const
        operator=operator,
        url=url,
        access=access,
        cadence=cadence,
        tier=3,
        egress="plain",  # D11
        connector=None,
        implemented=False,
        licence_id=licence.id,
        publish_state="ingest_only",
        host=host,
        attribution_text=body.get("attribution_text"),
    )
    db.add(source)
    db.flush()
    record_audit_event(
        db,
        subject_type="source",
        subject_id=_source_subject_uuid(source.id),
        event_type="created",
        actor=_require_user(ctx),
        reason=reason,
        before=None,
        after={"source_id": source.id, "licence_id": licence.id, "publish_state": "ingest_only"},
    )
    return build_envelope(
        _serialize_admin_source(source),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


def _require_user(ctx: AuthContext) -> User:
    """`require_admin()` already guarantees `ctx.user is not None`; this narrows the type for
    `record_audit_event`, which requires a concrete `User` (mypy strict)."""
    assert ctx.user is not None  # noqa: S101 - narrows a dependency-enforced invariant, not a runtime check
    return ctx.user


# =============================================================================== GET /sources/{id}
@router.get("/admin/v1/sources/{source_id}")
def admin_get_source(
    source_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    source = db.get(Source, source_id)
    if source is None:
        raise not_found(request.url.path)
    last_run = _last_runs_for(db, [source.id]).get(source.id)
    source.__dict__["_admin_last_run"] = last_run
    return build_envelope(
        _serialize_admin_source(source),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


# ============================================================================= PATCH /sources/{id}
_UPDATABLE_SOURCE_FIELDS = (
    "schedule_cron",
    "lag_days",
    "lag_overrides",
    "paused",
    "enrichment_enabled",
    "model_budget_usd_daily",
    "egress",
    "attribution_text",
)


@router.patch("/admin/v1/sources/{source_id}")
def admin_update_source(
    source_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    path = request.url.path
    reason = _require_reason(body, path)
    source = db.get(Source, source_id)
    if source is None:
        raise not_found(path)
    changed = [k for k in _UPDATABLE_SOURCE_FIELDS if k in body]
    if not changed:
        raise validation_error("reason", "at least one updatable field is required", path)
    if "lag_days" in body and body["lag_days"] is not None and not (0 <= int(body["lag_days"]) <= 30):
        raise validation_error("lag_days", "must be between 0 and 30", path)

    before = {k: _current_value(source, k) for k in changed}
    for key in changed:
        setattr(source, key, body[key])
    db.flush()
    after = {k: _current_value(source, k) for k in changed}
    record_audit_event(
        db,
        subject_type="source",
        subject_id=_source_subject_uuid(source.id),
        event_type="admin_edit",
        actor=_require_user(ctx),
        reason=reason,
        before={"source_id": source.id, **before},
        after={"source_id": source.id, **after},
    )
    last_run = _last_runs_for(db, [source.id]).get(source.id)
    source.__dict__["_admin_last_run"] = last_run
    return build_envelope(
        _serialize_admin_source(source),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


def _current_value(source: Source, field: str) -> Any:
    value = getattr(source, field)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return value


# ======================================================================== POST /sources/{id}/run
@router.post("/admin/v1/sources/{source_id}/run", status_code=202)
def admin_run_source(
    source_id: str,
    request: Request,
    body: dict[str, Any] | None,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
    runner: Annotated[SourceRunner, Depends(get_source_runner)],
) -> Any:
    path = request.url.path
    reason = _require_reason(body, path)  # D1
    source = db.get(Source, source_id)
    if source is None:
        raise not_found(path)
    if source.paused or not source.implemented:
        raise ProblemError(
            "conflict",
            "Source cannot be run",
            detail="The source is paused or has no implemented connector.",
        )
    already_running = db.scalar(
        select(SourceRun).where(SourceRun.source_id == source.id, SourceRun.status == "running")
    )
    if already_running is not None:
        raise ProblemError("conflict", "A run is already in progress for this source")

    trigger = (body or {}).get("trigger", "manual")
    if trigger not in ("manual", "backfill"):
        raise validation_error("trigger", "must be manual or backfill", path)
    run = runner.enqueue(db, source, trigger=trigger, requested_by=_require_user(ctx))
    record_audit_event(
        db,
        subject_type="source",
        subject_id=_source_subject_uuid(source.id),
        event_type="admin_edit",
        actor=_require_user(ctx),
        reason=reason,
        before={"source_id": source.id},
        after={"source_id": source.id, "run_id": public_id("run", run.id), "trigger": trigger},
    )
    return build_envelope(
        _serialize_source_run(run),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


# ============================================================== PUT /sources/{id}/publish-state
@router.put("/admin/v1/sources/{source_id}/publish-state")
def admin_set_source_publish_state(
    source_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    path = request.url.path
    reason = _require_reason(body, path)
    new_state = body.get("publish_state")
    if new_state not in SOURCE_PUBLISH_STATES:
        raise validation_error("publish_state", f"must be one of {SOURCE_PUBLISH_STATES}", path)
    source = db.get(Source, source_id)
    if source is None:
        raise not_found(path)

    if new_state != "ingest_only":
        missing = _licence_gate_missing(source.licence)
        if missing:
            raise ProblemError(
                "gate_unmet",
                "Licence gate unmet",
                detail=(
                    f"Source {source.id!r} cannot move to {new_state!r}: licence "
                    f"{source.licence.id!r} is missing {', '.join(missing)}."
                ),
            )

    before = {"publish_state": source.publish_state}
    source.publish_state = new_state
    db.flush()
    event_type = "unpublished" if new_state == "ingest_only" else "published"
    record_audit_event(
        db,
        subject_type="source",
        subject_id=_source_subject_uuid(source.id),
        event_type=event_type,
        actor=_require_user(ctx),
        reason=reason,
        before={"source_id": source.id, **before},
        after={"source_id": source.id, "publish_state": new_state},
    )
    last_run = _last_runs_for(db, [source.id]).get(source.id)
    source.__dict__["_admin_last_run"] = last_run
    return build_envelope(
        _serialize_admin_source(source),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


def _licence_gate_missing(licence: Licence) -> list[str]:
    """US-905 AC1 / invariant L1 (`Licence.gate_clear`), decomposed so `gate_unmet` can name each
    unmet condition (restricted reuse class, open gate flag, missing evidence) separately."""
    missing = []
    if licence.reuse_class not in ("open", "attribution"):
        missing.append(f"reuse_class (is {licence.reuse_class!r}, needs open or attribution)")
    if licence.gate_flag:
        missing.append(f"gate_flag (gate {licence.gate_name or 'unnamed'} is still open)")
    if licence.evidence_url is None or licence.evidence_retrieved_at is None or licence.classified_by is None:
        missing.append("legal evidence (evidence_url, evidence_retrieved_at, classified_by)")
    return missing


# =============================================================================== GET /source-runs
@router.get("/admin/v1/source-runs")
def admin_list_source_runs(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    check_allowed(request, {"limit", "cursor", "source_id", "status", "started_at[from]", "started_at[to]"})
    qp = request.query_params
    limit = clamp_limit(_int_param(request, "limit"))
    stmt = select(SourceRun)
    if v := qp.get("source_id"):
        stmt = stmt.where(SourceRun.source_id.in_(csv_param(v)))
    if v := qp.get("status"):
        stmt = stmt.where(SourceRun.status.in_(csv_param(v)))
    if v := qp.get("started_at[from]"):
        stmt = stmt.where(SourceRun.started_at >= _parse_dt(v, "started_at[from]", request.url.path))
    if v := qp.get("started_at[to]"):
        stmt = stmt.where(SourceRun.started_at <= _parse_dt(v, "started_at[to]", request.url.path))
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=SourceRun.started_at,
        id_column=SourceRun.id,
        ascending=False,
        cursor=qp.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [_serialize_source_run(r) for r in rows]
    return build_list_envelope(
        data,
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
        page=build_page(next_cursor, None, has_more),
    )


@router.get("/admin/v1/source-runs/{run_id}")
def admin_get_source_run(
    run_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    run = _find_source_run(db, run_id)
    if run is None:
        raise not_found(request.url.path)
    return build_envelope(
        _serialize_source_run(run),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


@router.get("/admin/v1/snapshots/{snapshot_id}")
def admin_get_snapshot(
    snapshot_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    snap = _find_snapshot(db, snapshot_id)
    if snap is None:
        raise not_found(request.url.path)
    return build_envelope(
        _serialize_snapshot(snap),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


# ======================================================================= PUT /licences/{id}/gate
@router.put("/admin/v1/licences/{licence_id}/gate")
def admin_set_licence_gate(
    licence_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin(roles=("legal",)))],
) -> Any:
    path = request.url.path
    reason = _require_reason(body, path)
    gate_flag = body.get("gate_flag")
    if not isinstance(gate_flag, bool):
        raise validation_error("gate_flag", "gate_flag (boolean) is required", path)
    licence = db.get(Licence, licence_id)
    if licence is None:
        raise not_found(path)

    reuse_class = body.get("reuse_class")
    if reuse_class is not None and reuse_class not in REUSE_CLASSES:
        raise validation_error("reuse_class", f"must be one of {REUSE_CLASSES}", path)
    evidence_url = body.get("evidence_url")
    evidence_retrieved_at_raw = body.get("evidence_retrieved_at")
    evidence_retrieved_at = (
        _parse_dt(evidence_retrieved_at_raw, "evidence_retrieved_at", path)
        if evidence_retrieved_at_raw is not None
        else None
    )
    classified_by = body.get("classified_by")

    if gate_flag is False:
        eff_url = evidence_url or licence.evidence_url
        eff_at = evidence_retrieved_at or licence.evidence_retrieved_at
        eff_by = classified_by or licence.classified_by
        missing = [
            name
            for name, val in (
                ("evidence_url", eff_url),
                ("evidence_retrieved_at", eff_at),
                ("classified_by", eff_by),
            )
            if not val
        ]
        if missing:
            raise ProblemError(
                "gate_unmet",
                "Licence gate unmet",
                detail=f"Clearing licence {licence_id!r}'s gate requires: {', '.join(missing)}.",
            )

    before = {
        "licence_id": licence.id,
        "reuse_class": licence.reuse_class,
        "gate_flag": licence.gate_flag,
        "gate_name": licence.gate_name,
    }

    reclassified = reuse_class is not None and reuse_class != licence.reuse_class
    target = _reclassify_licence(db, licence, str(reuse_class)) if reclassified else licence

    target.gate_flag = gate_flag
    if body.get("gate_name") is not None:
        target.gate_name = body["gate_name"]
    if evidence_url is not None:
        target.evidence_url = evidence_url
    if evidence_retrieved_at is not None:
        target.evidence_retrieved_at = evidence_retrieved_at
    if body.get("evidence_object_key") is not None:
        target.evidence_object_key = body["evidence_object_key"]
    if classified_by is not None:
        target.classified_by = classified_by
    if body.get("contract_ref") is not None:
        target.contract_ref = body["contract_ref"]
    if body.get("notes") is not None:
        target.notes = body["notes"]
    if gate_flag is False:
        target.gate_cleared_at = utcnow()
        target.gate_cleared_by = _require_user(ctx).id
    db.flush()

    if reclassified:
        event_type = "licence_reclassified"
    elif gate_flag is False:
        event_type = "gate_cleared"
    else:
        event_type = "admin_edit"
    record_audit_event(
        db,
        subject_type="licence",  # D3: not in SubjectType's enum today - a documented spec gap
        subject_id=_licence_subject_uuid(licence_id),
        event_type=event_type,
        actor=_require_user(ctx),
        reason=reason,
        before=before,
        after={
            "licence_id": target.id,
            "reuse_class": target.reuse_class,
            "gate_flag": target.gate_flag,
            "gate_name": target.gate_name,
        },
    )
    source_count = db.scalar(
        select(sa.func.count()).select_from(Source).where(Source.licence_id == target.id)
    )
    return build_envelope(
        _serialize_admin_licence(target, source_count=source_count or 0),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


def _reclassify_licence(db: Session, licence: Licence, reuse_class: str) -> Licence:
    """D5 / invariant L2: a reuse-class change creates a new licence row rather than mutating the
    old one in place, and every `Source` pointing at the old licence is repointed to the new one so
    future fetches and gate checks use the fresh classification; historical `snapshot` /
    `proposal_source` / `opportunity_source` rows already carry the licence id that applied at
    fetch time and are untouched."""
    new_id = _next_licence_id(db, licence.id, reuse_class)
    new_licence = Licence(
        id=new_id,
        name=licence.name,
        url=licence.url,
        reuse_class=reuse_class,
        attribution_required=licence.attribution_required,
        attribution_text=licence.attribution_text,
        requires_link_back=licence.requires_link_back,
        allows_derived_publication=licence.allows_derived_publication,
        allows_raw_publication=licence.allows_raw_publication,
        allows_api_redistribution=licence.allows_api_redistribution,
        allows_bulk_export=licence.allows_bulk_export,
        allows_commercial_use=licence.allows_commercial_use,
        share_alike=licence.share_alike,
        gate_name=licence.gate_name,
        evidence_url=licence.evidence_url,
        evidence_retrieved_at=licence.evidence_retrieved_at,
        evidence_object_key=licence.evidence_object_key,
        classified_by=licence.classified_by,
        contract_ref=licence.contract_ref,
        notes=licence.notes,
        quote_text=licence.quote_text,
    )
    db.add(new_licence)
    db.flush()
    db.execute(sa.update(Source).where(Source.licence_id == licence.id).values(licence_id=new_licence.id))
    db.flush()
    return new_licence


def _next_licence_id(db: Session, base_id: str, reuse_class: str) -> str:
    candidate = f"{base_id}-{reuse_class}"
    if db.get(Licence, candidate) is None:
        return candidate
    n = 2
    while db.get(Licence, f"{candidate}-{n}") is not None:
        n += 1
    return f"{candidate}-{n}"


# =================================================================================== GET /costs
@router.get("/admin/v1/costs")
def admin_get_costs(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    path = request.url.path
    check_allowed(request, {"day[from]", "day[to]", "source_id", "purpose", "group_by"})
    qp = request.query_params
    day_from = _parse_date(v, "day[from]", path) if (v := qp.get("day[from]")) else None
    day_to = _parse_date(v, "day[to]", path) if (v := qp.get("day[to]")) else None
    source_ids = csv_param(qp.get("source_id"))
    purposes = csv_param(qp.get("purpose"))
    if purposes:
        for p in purposes:
            if p not in _COST_PURPOSES:
                raise validation_error("purpose", f"must be one of {_COST_PURPOSES}", path)
    group_by_raw = csv_param(qp.get("group_by")) or ["source_id", "day"]
    for g in group_by_raw:
        if g not in _GROUP_BY_FIELDS:
            raise validation_error("group_by", f"must be one of {_GROUP_BY_FIELDS}", path)
    group_by = set(group_by_raw)

    mc_stmt = select(ModelCall)
    if day_from:
        mc_stmt = mc_stmt.where(ModelCall.created_at >= dt.datetime.combine(day_from, dt.time.min, dt.UTC))
    if day_to:
        mc_stmt = mc_stmt.where(ModelCall.created_at <= dt.datetime.combine(day_to, dt.time.max, dt.UTC))
    if source_ids:
        mc_stmt = mc_stmt.where(ModelCall.source_id.in_(source_ids))
    if purposes:
        mc_stmt = mc_stmt.where(ModelCall.purpose.in_(purposes))
    model_calls = list(db.scalars(mc_stmt).all())

    run_stmt = select(SourceRun)
    if day_from:
        run_stmt = run_stmt.where(SourceRun.started_at >= dt.datetime.combine(day_from, dt.time.min, dt.UTC))
    if day_to:
        run_stmt = run_stmt.where(SourceRun.started_at <= dt.datetime.combine(day_to, dt.time.max, dt.UTC))
    if source_ids:
        run_stmt = run_stmt.where(SourceRun.source_id.in_(source_ids))
    source_runs = list(db.scalars(run_stmt).all())

    buckets: dict[tuple[Any, ...], dict[str, Any]] = {}

    def _bucket(
        day: dt.date, source_id: str | None, purpose: str | None, model_alias: str | None
    ) -> dict[str, Any]:
        key = (
            day,
            source_id if "source_id" in group_by else None,
            purpose if "purpose" in group_by else None,
            model_alias if "model_alias" in group_by else None,
        )
        return buckets.setdefault(
            key,
            {
                "day": day,
                "purpose": key[2],
                "source_id": key[1],
                "model_alias": key[3],
                "cost_usd": 0.0,
                "model_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "_cache_hits": 0,
                "_cache_n": 0,
                "records_changed": 0,
                "_has_records_changed": False,
            },
        )

    for mc in model_calls:
        b = _bucket(mc.created_at.date(), mc.source_id, mc.purpose, mc.alias)
        b["cost_usd"] += float(mc.cost_usd)
        b["model_calls"] += 1
        b["input_tokens"] += mc.input_tokens
        b["output_tokens"] += mc.output_tokens
        b["_cache_n"] += 1
        b["_cache_hits"] += 1 if mc.cache_hit else 0

    for run in source_runs:
        b = _bucket(run.started_at.date(), run.source_id, None, None)
        b["cost_usd"] += float(run.cost_usd)
        b["model_calls"] += run.model_calls
        b["records_changed"] += run.rows_changed
        b["_has_records_changed"] = True

    rows = []
    total = {
        "cost_usd": 0.0,
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "_cache_hits": 0,
        "_cache_n": 0,
        "records_changed": 0,
        "_has_records_changed": False,
    }
    for b in sorted(buckets.values(), key=lambda x: (x["day"], x["source_id"] or "", x["purpose"] or "")):
        cache_hit_rate = (b["_cache_hits"] / b["_cache_n"]) if b["_cache_n"] else None
        records_changed = b["records_changed"] if b["_has_records_changed"] else None
        cost_per_changed = (
            b["cost_usd"] / records_changed if records_changed and records_changed > 0 else None
        )
        rows.append(
            {
                "day": b["day"].isoformat(),
                "purpose": b["purpose"],
                "source_id": b["source_id"],
                "model_alias": b["model_alias"],
                "cost_usd": round(b["cost_usd"], 6),
                "model_calls": b["model_calls"],
                "input_tokens": b["input_tokens"],
                "output_tokens": b["output_tokens"],
                "cache_hit_rate": cache_hit_rate,
                "records_changed": records_changed,
                "cost_per_changed_record": cost_per_changed,
            }
        )
        total["cost_usd"] += b["cost_usd"]
        total["model_calls"] += b["model_calls"]
        total["input_tokens"] += b["input_tokens"]
        total["output_tokens"] += b["output_tokens"]
        total["_cache_hits"] += b["_cache_hits"]
        total["_cache_n"] += b["_cache_n"]
        if b["_has_records_changed"]:
            total["records_changed"] += b["records_changed"]
            total["_has_records_changed"] = True

    total_day = day_to or (max((b["day"] for b in buckets.values()), default=utcnow().date()))
    total_records_changed = total["records_changed"] if total["_has_records_changed"] else None
    totals_row = {
        "day": total_day.isoformat(),
        "purpose": None,
        "source_id": None,
        "model_alias": None,
        "cost_usd": round(total["cost_usd"], 6),
        "model_calls": total["model_calls"],
        "input_tokens": total["input_tokens"],
        "output_tokens": total["output_tokens"],
        "cache_hit_rate": (total["_cache_hits"] / total["_cache_n"]) if total["_cache_n"] else None,
        "records_changed": total_records_changed,
        "cost_per_changed_record": (
            total["cost_usd"] / total_records_changed
            if total_records_changed and total_records_changed > 0
            else None
        ),
    }

    flagged_stmt = select(Source.id).where(
        Source.cost_per_changed_record_30d.is_not(None),
        Source.cost_per_changed_record_30d > DEFAULT_COST_THRESHOLD_USD,
    )
    if source_ids:
        flagged_stmt = flagged_stmt.where(Source.id.in_(source_ids))
    flagged_sources = list(db.scalars(flagged_stmt).all())

    window: dict[str, str] = {}
    if day_from:
        window["from"] = day_from.isoformat()
    if day_to:
        window["to"] = day_to.isoformat()

    data = {
        "window": window,
        "group_by": sorted(group_by, key=group_by_raw.index),
        "rows": rows,
        "totals": totals_row,
        "threshold_usd_per_changed_record": DEFAULT_COST_THRESHOLD_USD,
        "flagged_sources": flagged_sources,
    }
    return build_envelope(
        data, meta=build_meta(lag_days=0, tier="admin"), licence_summary=build_licence_summary([])
    )


# =================================================================================== GET /audit
@router.get("/admin/v1/audit")
def admin_list_audit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    path = request.url.path
    check_allowed(
        request,
        {"limit", "cursor", "since", "actor_user_id", "subject_type", "subject_id", "event_type"},
    )
    qp = request.query_params
    limit = clamp_limit(_int_param(request, "limit"))
    stmt = select(Event).where(Event.actor_type == "user")
    if v := qp.get("since"):
        try:
            stmt = stmt.where(Event.seq > int(v))
        except ValueError:
            stmt = stmt.where(Event.observed_at > _parse_dt(v, "since", path))
    if v := qp.get("actor_user_id"):
        user = db.scalar(select(User).where(User.public_id == v))
        stmt = stmt.where(Event.actor_user_id == (user.id if user else _uuid.uuid4()))
    if v := qp.get("subject_type"):
        stmt = stmt.where(Event.subject_type.in_(csv_param(v)))
    if v := qp.get("subject_id"):
        candidate = _resolve_any_subject_uuid(v)
        stmt = stmt.where(Event.subject_id == (candidate or _uuid.uuid4()))
    if v := qp.get("event_type"):
        stmt = stmt.where(Event.event_type.in_(csv_param(v)))

    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=Event.seq,
        id_column=Event.id,
        ascending=False,
        cursor=qp.get("cursor"),
        limit=limit,
        instance=path,
    )
    data = [_serialize_admin_audit_event(e) for e in rows]
    return build_list_envelope(
        data,
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
        page=build_page(next_cursor, None, has_more),
    )


def _resolve_any_subject_uuid(value: str) -> _uuid.UUID | None:
    """Best-effort decode of a `subject_id` filter value: a graph public id (`prop_`, `mat_`, ...)
    via the standard Crockford scheme, else treated as a literal `source_id`/`licence_id` hashed
    the same deterministic way the audit writers above do (D2/D4)."""
    for prefix in ("prop", "opp", "org", "mat", "evt", "doc"):
        candidate = _decode_crockford_uuid(value, prefix)
        if candidate is not None:
            return candidate
    return _source_subject_uuid(value)


__all__ = ["router"]
