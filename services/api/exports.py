"""CSV exports (US-603; docs/23 §3.2 `/v1/exports`, §6 quotas, §10 attribution columns):
`POST /v1/exports`, `GET /v1/exports`, `GET /v1/exports/{id}`, the authenticated download route,
and the `Accept: text/csv` path onto the same generator for the three list endpoints (docs/23 §1).

**Generation is synchronous, inside the request, for v1.** Measured end to end through
`POST /v1/exports` on the real `data/normalized` load in SQLite (10,409 proposals, 707
opportunities; 12,000 synthetic events added for the event case; a shared 4-core machine at load
average 4-6): a 10,000-row proposal export at the cap takes 2.5-3.6 s, a 10,000-row event export
1.6-2.1 s, a 2,333-row filtered one under 1 s (`services/README.md` "Exports, bulk and documents" has
the table and the before/after of the two loader fixes that got it there). That is well inside
any request timeout in the path (Cloudflare's is 100 s), so a queue would add a worker hop and a
polling round trip for no latency the caller can feel. The function that does the work,
`generate_export`, takes only a session and the `Export` row so it can become the body of a
Procrastinate job (`infra/scheduler/app.py`'s `alert_tick` pattern) the day a plan's cap or a
slower backend makes it necessary; the API shape already says `queued -> running -> ready`, so
nothing changes for a client that polls. The `202` response carries the finished row
(`status: ready` or `failed`), which the `ExportStatus` enum admits.

**Storage is a local directory** -- `EXPORT_DIR` (docs/60-deployment.md §5.2; default
`data/exports/` under the repository, which `.gitignore` does not list yet: add it before anyone runs
an export from a working copy) -- keyed by `Export.object_key`.
Production follow-up, not done here: the same `object_key` on Cloudflare R2 (docs/20 §4.1's
object store), `download_url` becoming a pre-signed R2 URL valid until `expires_at`, and a sweep
that deletes expired objects. Until then `download_url` points at `GET /v1/exports/{id}/download`
on this API, which streams the file to its owner only; the row's `expires_at` (24 h) is honoured
by that route and by `GET /v1/exports/{id}` (`status` flips to `expired`).

**Per-row provenance and the licence header** (US-105 AC2, US-603 AC2, docs/23 §10): every row
ends with `source_id, source_name, source_record_id, source_url, retrieved_at, licence,
licence_name, reuse_class, attribution_text`; the file opens with a `#` comment block carrying the
licence summary (one line per source present) and the attribution line. A multi-source record is
one row whose provenance columns come from its first active source link whose licence permits
bulk export (`services/api/resource_queries.py::redistributable_link`), with `source_count` saying
how many links the tier may see -- one row per record keeps the row cap meaning "records". Every
derived column is the record's served view narrowed to bulk-exportable sources
(`services/api/visibility.py::GatedRecord`, 2026-10-06): no value comes from a source the tier may
not read or whose licence does not permit bulk export, raw columns are empty under a derived-only
licence, and exactly the sources that supply the row are credited in the header block. A record
with no such link is not exported at all: its row would have no provenance to print (CLAUDE.md:
never a record without it). Event rows carry the event's own quartet; their `before`/`after` are
the same values `GET /v1/events` serves. Tier visibility is `services/api/visibility.py`'s
own predicate, reached through the list endpoints' filter functions; nothing here re-states it.

**Logged per user** (US-603 AC3, metric M-6): the `export` row itself (`user_id`, `api_key_id`,
`entity`, `query`, `row_count`, `tier`, `created_at`, `error`) plus one structured log line per
export; a key-only caller is attributed to the key's creator, as `/v1/me` and webhooks already do.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import logging
import os
import pathlib
import time
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.api.auth import AuthContext, require_entitlement
from services.api.common import API_HOST, TERMS_URL, WEB_HOST, iso, utcnow
from services.api.deps import get_db
from services.api.errors import ProblemError, not_found, validation_error
from services.api.geo import effective_placement
from services.api.pagination import clamp_limit, paginate
from services.api.params import check_allowed, int_param, sort_spec
from services.api.pro import _rate_limit_headers
from services.api.ratelimit import PlanQuota, plan_quota
from services.api.records import check_budget_sort
from services.api.resource_queries import (
    DEFAULT_SORTS,
    SORT_ALLOWLISTS,
    Resource,
    lean_load_options,
    normalise_resource,
    redistributable_link,
    resource_model,
    resource_statement,
    subject_infos,
    synthetic_request,
    validate_export_query,
)
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
    licence_summary_row,
)
from services.api.visibility import gated_record
from services.db.models import (
    Event,
    Export,
    Opportunity,
    OpportunitySource,
    Proposal,
    ProposalSource,
    SavedSearch,
    Source,
    User,
)
from services.ids import public_id

logger = logging.getLogger("services.api.exports")
router = APIRouter()

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_EXPORT_DIR = REPO_ROOT / "data" / "exports"
#: How long a generated file is downloadable (docs/23 §3.2 "valid until `expires_at`").
EXPORT_TTL = dt.timedelta(hours=24)


def export_dir() -> pathlib.Path:
    """`EXPORT_DIR` from the environment (docs/60-deployment.md §5.2), read per call so a test's
    `monkeypatch.setenv` takes effect; created on first use."""
    path = pathlib.Path(os.environ.get("EXPORT_DIR") or DEFAULT_EXPORT_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def export_path(object_key: str) -> pathlib.Path:
    """The stored file for `object_key`, refusing any key that would resolve outside the export
    directory (keys are generated here as `<export public id>.csv`, so this is defence in depth
    against a hand-edited row, not a path a caller can supply)."""
    root = export_dir().resolve()
    path = (root / object_key).resolve()
    if root not in path.parents:
        raise ValueError(f"export object key escapes the export directory: {object_key!r}")
    return path


# ------------------------------------------------------------------------------------ columns
def _bulk_exportable(source: Source) -> bool:
    return bool(source.licence.allows_bulk_export)


def _num(value: Any) -> Any:
    return float(value) if value is not None else None


def _proposal_row(p: Proposal) -> dict[str, Any]:
    placement = effective_placement(p.location) if p.location else None
    lon, lat = placement.geom if placement and placement.geom else (None, None)
    return {
        "public_id": p.public_id,
        "slug": p.slug,
        "url": f"{WEB_HOST}/proposals/{p.slug}",
        "kind": p.kind,
        "name_canonical": p.name_canonical,
        "sponsor_public_id": p.sponsor.public_id if p.sponsor else None,
        "sponsor_name": p.sponsor.name_canonical if p.sponsor else None,
        "technology": p.technology,
        "technology_raw": p.technology_raw,
        "capacity_mw": _num(p.capacity_mw),
        "storage_mwh": _num(p.storage_mwh),
        "jurisdiction": p.jurisdiction,
        "iso": p.iso,
        "lifecycle_state": p.lifecycle_state,
        "status_raw": p.status_raw,
        "proposed_online_date": iso(p.proposed_online_date),
        "first_seen": iso(p.first_seen),
        "last_changed": iso(p.last_changed),
        "min_reuse_class": p.min_reuse_class,
        "source_count": p.source_count,
        "placement_precision": placement.precision if placement else None,
        "county_name": p.location.county_name if p.location else None,
        "county_fips": p.location.county_fips if p.location else None,
        "state_code": p.location.state_code if p.location else None,
        "country": p.location.country if p.location else None,
        "latitude": lat,
        "longitude": lon,
    }


def _opportunity_row(o: Opportunity) -> dict[str, Any]:
    return {
        "public_id": o.public_id,
        "slug": o.slug,
        "url": f"{WEB_HOST}/opportunities/{o.slug}",
        "kind": o.kind,
        "title": o.title,
        "issuer_public_id": o.issuer.public_id if o.issuer else None,
        "issuer_name": o.issuer.name_canonical if o.issuer else None,
        "jurisdiction": o.jurisdiction,
        "technologies": ";".join(o.technologies or []),
        "capacity_sought_mw": _num(o.capacity_sought_mw),
        "budget_amount": _num(o.budget_amount),
        "budget_currency": o.budget_currency,
        "open_at": iso(o.open_at),
        "due_at": iso(o.due_at),
        "status": o.status,
        "status_raw": o.status_raw,
        "first_seen": iso(o.first_seen),
        "last_changed": iso(o.last_changed),
        "min_reuse_class": o.min_reuse_class,
        "source_count": o.source_count,
    }


def _event_row(e: Event, subject: dict[str, str]) -> dict[str, Any]:
    """`subject` is `resource_queries.subject_infos`' entry for the event (batched per export)."""
    return {
        "id": public_id("evt", e.id),
        "seq": e.seq,
        "subject_type": e.subject_type,
        "subject_id": subject["subject_public_id"],
        "subject_name": subject["subject_name"],
        "event_type": e.event_type,
        "observed_at": iso(e.observed_at),
        "recorded_at": iso(e.recorded_at),
        "published_at": iso(e.published_at),
        "before": json.dumps(e.before, sort_keys=True, default=str) if e.before is not None else None,
        "after": json.dumps(e.after, sort_keys=True, default=str) if e.after is not None else None,
        "changed_keys": ";".join(e.changed_keys or []),
        "actor_type": e.actor_type,
        "reason": e.reason,
    }


#: The derived columns per resource, in file order (`columns` in `ExportCreate` may narrow them).
#: Stated as data and pinned equal to the row builders' key order by `tests/test_api_exports.py`
#: rather than derived at import from a shape-only object.
DERIVED_COLUMNS: dict[Resource, tuple[str, ...]] = {
    "proposal": (
        "public_id",
        "slug",
        "url",
        "kind",
        "name_canonical",
        "sponsor_public_id",
        "sponsor_name",
        "technology",
        "technology_raw",
        "capacity_mw",
        "storage_mwh",
        "jurisdiction",
        "iso",
        "lifecycle_state",
        "status_raw",
        "proposed_online_date",
        "first_seen",
        "last_changed",
        "min_reuse_class",
        "source_count",
        "placement_precision",
        "county_name",
        "county_fips",
        "state_code",
        "country",
        "latitude",
        "longitude",
    ),
    "opportunity": (
        "public_id",
        "slug",
        "url",
        "kind",
        "title",
        "issuer_public_id",
        "issuer_name",
        "jurisdiction",
        "technologies",
        "capacity_sought_mw",
        "budget_amount",
        "budget_currency",
        "open_at",
        "due_at",
        "status",
        "status_raw",
        "first_seen",
        "last_changed",
        "min_reuse_class",
        "source_count",
    ),
    "event": (
        "id",
        "seq",
        "subject_type",
        "subject_id",
        "subject_name",
        "event_type",
        "observed_at",
        "recorded_at",
        "published_at",
        "before",
        "after",
        "changed_keys",
        "actor_type",
        "reason",
    ),
}
#: Always appended, never narrowable (US-105 AC2: "on every row").
PROVENANCE_COLUMNS = (
    "source_id",
    "source_name",
    "source_record_id",
    "source_url",
    "retrieved_at",
    "licence",
    "licence_name",
    "reuse_class",
    "attribution_text",
)


def _provenance_columns_for_link(
    link: ProposalSource | OpportunitySource | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """`(csv columns, licence-summary row)` for a record's redistributable source link."""
    if link is None:
        return dict.fromkeys(PROVENANCE_COLUMNS), None
    source, licence = link.source, link.source.licence
    allow_record_id = licence.allows_raw_publication or licence.reuse_class == "open"
    cols = {
        "source_id": source.id,
        "source_name": source.name,
        "source_record_id": link.source_record_id if allow_record_id else None,
        "source_url": link.source_url,
        "retrieved_at": iso(link.retrieved_at),
        "licence": licence.id,
        "licence_name": licence.name,
        "reuse_class": licence.reuse_class,
        "attribution_text": source.attribution_text or licence.attribution_text,
    }
    return cols, licence_summary_row(source, licence, link.retrieved_at)


def _provenance_columns_for_event(e: Event) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if e.source is None or e.licence is None:
        return dict.fromkeys(PROVENANCE_COLUMNS), None
    cols = {
        "source_id": e.source.id,
        "source_name": e.source.name,
        "source_record_id": None,
        "source_url": e.source_url or e.source.url,
        "retrieved_at": iso(e.retrieved_at or e.observed_at),
        "licence": e.licence.id,
        "licence_name": e.licence.name,
        "reuse_class": e.licence.reuse_class,
        "attribution_text": e.source.attribution_text or e.licence.attribution_text,
    }
    return cols, licence_summary_row(e.source, e.licence, e.retrieved_at)


# ---------------------------------------------------------------------------------- generation
def _checked_statement(
    resource: Resource, params: Mapping[str, Any], *, db: Session, tier: str, instance: str
) -> tuple[Any, str, bool]:
    """The export's filtered statement and its sort, built but not run. Every value the list would
    refuse (a non-numeric bound, an unknown sort, a cross-currency budget sort) raises the list's
    own `400` here. `POST /v1/exports` and the `Accept: text/csv` twin call it before an export row
    exists, so a bad request is a `400` and not a `failed` export (or, on the CSV path, a `503`
    for what is the caller's error; 2026-09-27). Generation calls it again, as its only path."""
    stmt = resource_statement(
        resource,
        params,
        db=db,
        entitlement=tier,
        redistribution="allows_bulk_export",
        instance=instance,
    )
    request = synthetic_request(params, path=instance)
    field, ascending = sort_spec(request, SORT_ALLOWLISTS[resource], DEFAULT_SORTS[resource])
    if resource == "opportunity":
        # The one ordering path every export and `Accept: text/csv` file goes through: never order
        # budgets across currencies, whatever reached it (records.check_budget_sort).
        check_budget_sort(
            request.query_params.get("sort"), request.query_params.get("budget_currency"), instance
        )
    return stmt, field, ascending


def _ordered_capped_statement(export: Export, db: Session) -> Any:
    resource: Resource = export.entity  # type: ignore[assignment]
    params = dict(export.query or {})
    instance = f"/v1/exports/{export.public_id}"
    stmt, field, ascending = _checked_statement(resource, params, db=db, tier=export.tier, instance=instance)
    model = resource_model(resource)
    sort_column = getattr(model, field)
    order = (sort_column.asc() if ascending else sort_column.desc()).nulls_last()
    return (
        stmt.options(*lean_load_options(resource, identifiers=False))
        .order_by(order, model.id.asc())
        .limit(export.row_cap + 1)
    )


def _header_block(export: Export, licence_summary: dict[str, Any], *, row_count: int) -> list[str]:
    """The leading `#` block (docs/23 §10). Line 1 alone is the licence summary a reader needs:
    the export, its size, and every licence id present; the lines after it credit each source."""
    now = utcnow()
    licence_ids = sorted({s["licence_id"] for s in licence_summary["sources"]})
    lines = [
        f"# Infraque CSV export {export.public_id} · entity={export.entity} · tier={export.tier} "
        f"· licences={','.join(licence_ids) or 'none'} "
        f"· generated_at={iso(now)} · data_as_of={iso(now)} · rows={row_count} "
        f"· row_cap={export.row_cap} · truncated={'true' if export.truncated else 'false'}",
        f"# {licence_summary['attribution_line']}",
    ]
    for s in licence_summary["sources"]:
        credit = s.get("attribution_text") or "no credit line stated"
        lines.append(
            f"# {s['source_id']}: {s['licence_name']} ({s['reuse_class']}) — {credit}; "
            f"licence_url={s.get('licence_url') or 'n/a'}; "
            f"requires_link_back={str(s['requires_link_back']).lower()}; "
            f"records={s['record_count']}; retrieved_at_max={s['retrieved_at_max']}"
        )
    lines.append(f"# {licence_summary['redistribution']}")
    lines.append(f"# terms={TERMS_URL}")
    return lines


def _write_csv(export: Export, db: Session, rows_iter: Any, columns: list[str]) -> tuple[int, dict[str, Any]]:
    """Rows to a body buffer while the licence rows are collected, then the header block and body
    to the final file (the block depends on which sources turned up). Returns
    `(row_count, licence_summary)`; sets `object_key`, `byte_size`, `sha256`, `truncated`."""
    resource: Resource = export.entity  # type: ignore[assignment]
    body = io.StringIO()
    writer = csv.writer(body, lineterminator="\n")
    writer.writerow([*columns, *PROVENANCE_COLUMNS])
    licence_rows: list[dict[str, Any]] = []
    count = 0
    rows = list(rows_iter)
    subjects = subject_infos(db, rows, export.tier) if resource == "event" else {}
    for row in rows:
        if count >= export.row_cap:
            export.truncated = True
            break
        if resource == "event":
            derived = _event_row(row, subjects[row.subject_id])
            prov, lic = _provenance_columns_for_event(row)
            if lic is not None:
                licence_rows.append(lic)
        else:
            # The record's served view at the export's tier, narrowed to sources whose licence
            # permits bulk export (`services/api/visibility.py::GatedRecord`, 2026-10-06): every
            # derived column comes from such a source, and only those sources are credited. A
            # source the tier may not read is neither printed nor credited (docs/21 §8 items 3-5).
            view = gated_record(row, export.tier, _bulk_exportable)
            derived = _proposal_row(view) if resource == "proposal" else _opportunity_row(view)
            prov, _lic = _provenance_columns_for_link(
                redistributable_link(list(view.sources), "allows_bulk_export")
            )
            licence_rows.extend(
                licence_summary_row(link.source, link.source.licence, link.retrieved_at)
                for link in view.sources
            )
        writer.writerow([*(derived[c] for c in columns), *(prov[c] for c in PROVENANCE_COLUMNS)])
        count += 1
    licence_summary = build_licence_summary(licence_rows)
    text = "\n".join(_header_block(export, licence_summary, row_count=count)) + "\n" + body.getvalue()
    data = text.encode("utf-8")
    object_key = f"{export.public_id}.csv"
    export_path(object_key).write_bytes(data)
    export.object_key = object_key
    export.byte_size = len(data)
    export.sha256 = hashlib.sha256(data).hexdigest()
    return count, licence_summary


def generate_export(db: Session, export: Export, *, columns: list[str] | None = None) -> Export:
    """Run one export to completion: `running -> ready | failed`, file written, row fields set.
    Job-shaped on purpose (session + row in, row out): see the module docstring."""
    started = time.perf_counter()
    export.status = "running"
    export.started_at = utcnow()
    db.flush()
    resource: Resource = export.entity  # type: ignore[assignment]
    cols = list(columns) if columns else list(DERIVED_COLUMNS[resource])
    try:
        stmt = _ordered_capped_statement(export, db)
        count, _summary = _write_csv(export, db, db.scalars(stmt), cols)
        export.row_count = count
        export.status = "ready"
        export.completed_at = utcnow()
        export.expires_at = export.completed_at + EXPORT_TTL
    except ProblemError as exc:
        export.status = "failed"
        export.error = exc.detail or exc.title
        export.completed_at = utcnow()
    except Exception as exc:  # the row records the failure; the caller decides what to raise
        export.status = "failed"
        export.error = f"{type(exc).__name__}: {exc}"[:500]
        export.completed_at = utcnow()
        logger.exception("export %s failed", export.public_id)
    db.flush()
    logger.info(
        "export public_id=%s status=%s user_id=%s account_id=%s api_key_id=%s entity=%s rows=%s "
        "truncated=%s row_cap=%s tier=%s duration_ms=%d",
        export.public_id,
        export.status,
        export.user_id,
        export.account_id,
        export.api_key_id,
        export.entity,
        export.row_count,
        export.truncated,
        export.row_cap,
        export.tier,
        int((time.perf_counter() - started) * 1000),
    )
    return export


# ------------------------------------------------------------------------------------ helpers
def _caller_user(db: Session, ctx: AuthContext) -> User | None:
    if ctx.user is not None:
        return ctx.user
    if ctx.api_key is not None:
        return db.get(User, ctx.api_key.created_by_user_id)
    return None


def _seconds_to_midnight_utc(now: dt.datetime) -> int:
    tomorrow = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


def _enforce_daily_quota(db: Session, user: User, quota: PlanQuota, *, instance: str) -> None:
    """docs/23 §6 "5 exports / day" per user, midnight UTC to midnight UTC; a failed attempt does
    not count. Breach is `429 quota_exceeded` with `Retry-After` to midnight UTC (docs/23 §8)."""
    if quota.exports_per_day is None or quota.export_rows_max is None:
        raise ProblemError(
            "forbidden_tier",
            "Exports need a Pro or API plan",
            detail="This operation needs at least the 'pro' entitlement.",
            instance=instance,
        )
    now = utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    used = (
        db.scalar(
            select(func.count())
            .select_from(Export)
            .where(Export.user_id == user.id, Export.created_at >= day_start, Export.status != "failed")
        )
        or 0
    )
    if used >= quota.exports_per_day:
        raise ProblemError(
            "quota_exceeded",
            "Daily export quota exhausted",
            detail=f"{quota.exports_per_day} exports per day on this plan; resets at midnight UTC.",
            instance=instance,
            headers={"Retry-After": str(_seconds_to_midnight_utc(now))},
        )


def _validate_columns(resource: Resource, columns: Any, *, instance: str) -> list[str] | None:
    if columns is None:
        return None
    if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        raise validation_error("columns", "columns must be a list of column names", instance)
    unknown = [c for c in columns if c not in DERIVED_COLUMNS[resource]]
    if unknown:
        raise validation_error(
            "columns",
            f"unknown column(s) for {resource}: {', '.join(unknown)}; "
            f"available: {', '.join(DERIVED_COLUMNS[resource])}",
            instance,
        )
    return [c for c in DERIVED_COLUMNS[resource] if c in columns]


def start_export(
    db: Session,
    ctx: AuthContext,
    *,
    resource: Resource,
    query: dict[str, Any],
    instance: str,
    columns: list[str] | None = None,
) -> Export:
    """Quota, cap, row, generation -- the one path both `POST /v1/exports` and the
    `Accept: text/csv` list twin go through, so both are logged and both are capped."""
    user = _caller_user(db, ctx)
    if user is None or ctx.account is None:
        raise not_found(instance)
    quota = plan_quota(ctx.entitlement)
    _enforce_daily_quota(db, user, quota, instance=instance)
    assert quota.export_rows_max is not None  # noqa: S101 - narrowed by _enforce_daily_quota
    export = Export(
        public_id="",
        account_id=ctx.account.id,
        user_id=user.id,
        api_key_id=ctx.api_key.id if ctx.api_key else None,
        entity=resource,
        query=query,
        tier=ctx.entitlement,
        status="queued",
        row_cap=quota.export_rows_max,
    )
    db.add(export)
    db.flush()
    export.public_id = public_id("exp", export.id)
    db.flush()
    return generate_export(db, export, columns=columns)


def _refresh_expiry(export: Export) -> None:
    if export.status == "ready" and export.expires_at is not None and _aware(export.expires_at) <= utcnow():
        export.status = "expired"
        if export.object_key:
            export_path(export.object_key).unlink(missing_ok=True)


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def serialize_export(export: Export) -> dict[str, Any]:
    ready = export.status == "ready"
    return {
        "export_id": export.public_id,
        "entity": export.entity,
        "query": export.query or {},
        "status": export.status,
        "row_cap": export.row_cap,
        "row_count": export.row_count,
        "truncated": bool(export.truncated),
        "download_url": f"{API_HOST}/v1/exports/{export.public_id}/download" if ready else None,
        "expires_at": iso(export.expires_at) if ready else None,
        "error": export.error,
        "created_at": iso(export.created_at),
        "completed_at": iso(export.completed_at),
    }


def _owned_export(db: Session, ctx: AuthContext, export_id: str, instance: str) -> Export:
    user = _caller_user(db, ctx)
    export = db.scalar(select(Export).where(Export.public_id == export_id))
    if export is None or user is None or export.user_id != user.id:
        raise not_found(instance)
    _refresh_expiry(export)
    return export


def _envelope(data: Any, ctx: AuthContext) -> dict[str, Any]:
    return build_envelope(
        data, meta=build_meta(lag_days=0, tier=ctx.entitlement), licence_summary=build_licence_summary([])
    )


# ------------------------------------------------------------------------------------- routes
@router.post("/v1/exports", status_code=202)
def create_export(
    request: Request,
    response: Response,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_entitlement("pro"))],
) -> Any:
    for k, v in _rate_limit_headers(request, ctx).items():
        response.headers[k] = v
    instance = request.url.path
    resource = normalise_resource(body.get("entity") or body.get("resource"))
    if resource is None:
        raise validation_error("entity", "entity must be one of proposal, opportunity, event", instance)
    query = body.get("query")
    if query is None and (saved_search_id := body.get("saved_search_id")):
        search = db.scalar(select(SavedSearch).where(SavedSearch.public_id == saved_search_id))
        if search is None or ctx.account is None or search.account_id != ctx.account.id:
            raise not_found(instance, "No such saved search.")
        if search.entity != resource:
            raise validation_error("saved_search_id", "the saved search is for a different entity", instance)
        query = search.query
    if query is None:
        raise validation_error("query", "query (or saved_search_id) is required", instance)
    validated = validate_export_query(resource, query, instance=instance)
    _checked_statement(resource, validated, db=db, tier=ctx.entitlement, instance=instance)
    columns = _validate_columns(resource, body.get("columns"), instance=instance)
    export = start_export(db, ctx, resource=resource, query=validated, instance=instance, columns=columns)
    return _envelope(serialize_export(export), ctx)


@router.get("/v1/exports")
def list_exports(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_entitlement("pro"))],
) -> Any:
    check_allowed(request, {"limit", "cursor"})
    for k, v in _rate_limit_headers(request, ctx).items():
        response.headers[k] = v
    user = _caller_user(db, ctx)
    if user is None:
        raise not_found(request.url.path)
    rows, next_cursor, has_more = paginate(
        db,
        select(Export).where(Export.user_id == user.id),
        sort_column=Export.created_at,
        id_column=Export.id,
        ascending=False,
        cursor=request.query_params.get("cursor"),
        limit=clamp_limit(int_param(request, "limit")),
        instance=request.url.path,
    )
    for row in rows:
        _refresh_expiry(row)
    return build_list_envelope(
        [serialize_export(e) for e in rows],
        meta=build_meta(lag_days=0, tier=ctx.entitlement),
        licence_summary=build_licence_summary([]),
        page=build_page(next_cursor, None, has_more),
    )


@router.get("/v1/exports/{export_id}")
def get_export(
    export_id: str,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_entitlement("pro"))],
) -> Any:
    for k, v in _rate_limit_headers(request, ctx).items():
        response.headers[k] = v
    export = _owned_export(db, ctx, export_id, request.url.path)
    return _envelope(serialize_export(export), ctx)


@router.get("/v1/exports/{export_id}/download")
def download_export(
    export_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_entitlement("pro"))],
) -> Response:
    """The v1 stand-in for a pre-signed object-store URL (module docstring): the owner's session
    or key is the credential; anyone else gets the same `404` an unknown id gets."""
    export = _owned_export(db, ctx, export_id, request.url.path)
    if export.status != "ready" or not export.object_key:
        raise not_found(
            request.url.path, "This export has no downloadable file (not ready, failed or expired)."
        )
    path = export_path(export.object_key)
    if not path.exists():
        raise not_found(request.url.path, "The export file is no longer available.")
    return FileResponse(
        path,
        media_type="text/csv; charset=utf-8",
        filename=export.object_key,
        headers={"X-Export-Id": export.public_id, "Cache-Control": "private, no-store"},
    )


# -------------------------------------------------------------------- Accept: text/csv (docs/23 §1)
_PAGE_PARAMS = ("limit", "cursor", "include")


def csv_list_response(request: Request, db: Session, ctx: AuthContext, resource: Resource) -> Response:
    """The list endpoint's CSV twin: the same filters as the JSON page (page parameters dropped),
    through `start_export` so it is quota-counted, row-capped and logged exactly like `POST
    /v1/exports`, and the finished file is returned inline instead of a link. Anonymous callers
    get `401` (docs/23 §1: "returns an export (Pro+)"), a free account `403`. The caller has
    already run the list endpoint's own `check_allowed`, so an unknown filter is a `400` here too."""
    if not ctx.is_authenticated:
        raise ProblemError(
            "unauthenticated", "CSV export needs a Pro or API credential", instance=request.url.path
        )
    if plan_quota(ctx.entitlement).exports_per_day is None:
        raise ProblemError(
            "forbidden_tier",
            "CSV export needs a Pro or API plan",
            detail="This operation needs at least the 'pro' entitlement.",
            instance=request.url.path,
        )
    headers = _rate_limit_headers(request, ctx)
    query = {k: v for k, v in request.query_params.items() if k not in _PAGE_PARAMS}
    _checked_statement(resource, query, db=db, tier=ctx.entitlement, instance=request.url.path)
    export = start_export(db, ctx, resource=resource, query=query, instance=request.url.path)
    if export.status != "ready" or not export.object_key:
        # Keep the failed row (the per-user log, US-603 AC3) although the response is an error:
        # `get_db` rolls the request's session back on any exception.
        db.commit()
        raise ProblemError(
            "unavailable",
            "Export failed",
            detail=export.error or "The export could not be generated.",
            instance=request.url.path,
        )
    return FileResponse(
        export_path(export.object_key),
        media_type="text/csv; charset=utf-8",
        filename=export.object_key,
        headers={**headers, "X-Export-Id": export.public_id, "Cache-Control": "private, no-store"},
    )


__all__ = [
    "DERIVED_COLUMNS",
    "EXPORT_TTL",
    "PROVENANCE_COLUMNS",
    "csv_list_response",
    "export_dir",
    "export_path",
    "generate_export",
    "router",
    "serialize_export",
    "start_export",
]
