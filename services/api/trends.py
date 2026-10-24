"""`GET /v1/trends`: named series from the official archives and the daily record (`docs/24-trends.md` §10).

Without `metric`, the catalogue: every series the caller may read, each with its source, licence,
method, frequency and first and last dates. With `metric`, one series (`by` breaks it down,
`from` and `to` bound it), every point with its basis, provenance and like-for-like changes. The
series logic, the like-for-like rule and what a document holds are `services/trends/series.py`;
this module decides what the caller may see and wraps it in the attribution envelope.

**Visibility.** A monthly series is read only while its source is visible at the caller's tier
(`visibility.source_visible`: the source's own `publish_state`, and its licence class publishable
under the platform posture), so an operator who takes a source off the surface takes its series
with it. A source with no `source` row yet (the history job has never run against this store) has
no series. The daily record's documents are counts the public tier served; under the `commercial`
posture only those computed under `commercial` are read (`series.publishable_postures`).

**Attribution** (CLAUDE.md: "Attribution renders automatically"). The licence summary has one row
per document the response read, per source: a monthly series' months, each under its source; a
daily series' days, each under every source that contributed to that day's figures, as the day's
document recorded them (`series.daily_source_rows`), so a count built on CAISO's or NYISO's rows
carries "Source: California ISO" or "Source: NYISO". A source the store still holds is credited
as it is today (its row's credit, as every other route credits it); one it no longer holds, as the
document recorded it. `record_count` is the number of documents the source contributed to.

Reads the documents under the connector data root (`INFRAQUE_DATA_DIR`), which the single-host
deployment mounts read-only into the api container (`infra/compose/compose.single.yml`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from services.api.auth import AuthContext, get_auth_context
from services.api.deps import get_db
from services.api.errors import validation_error
from services.api.params import check_allowed
from services.api.serialize import build_envelope, build_licence_summary, build_meta, licence_summary_row
from services.api.visibility import source_visible
from services.db.models import Source
from services.posture import platform_posture
from services.trends import series as trends

router = APIRouter()

PARAMS = {"metric", "by", "from", "to"}


def _visible_sources(db: Session, entitlement: str) -> tuple[dict[str, trends.SourceView], dict[str, Source]]:
    """The monthly sources the caller may read, as the series module takes them, and their rows."""
    views: dict[str, trends.SourceView] = {}
    rows: dict[str, Source] = {}
    for source in db.scalars(sa.select(Source).where(Source.id.in_(trends.source_ids()))):
        if not source_visible(source, entitlement):
            continue
        licence = source.licence
        views[source.id] = trends.SourceView(
            source_id=source.id,
            name=source.name,
            operator=source.operator,
            url=source.url,
            licence={
                "licence_id": licence.id,
                "name": licence.name,
                "url": licence.url,
                "reuse_class": licence.reuse_class,
            },
            attribution_text=source.attribution_text or licence.attribution_text,
        )
        rows[source.id] = source
    return views, rows


def _served(data: Any, *, catalogue: bool) -> tuple[dict[str, set[str] | None], set[str] | None]:
    """What the response read: per monthly source the months (None: all), and the daily documents
    (None: all; an empty set: none)."""
    if catalogue:
        monthly: dict[str, set[str] | None] = {
            d["source"]["source_id"]: None for d in data if d["frequency"] == "monthly"
        }
        daily: set[str] | None = None if any(d["frequency"] == "daily" for d in data) else set()
        return monthly, daily
    points = [p for line in data["lines"] for p in line["points"]]
    if data["frequency"] == "monthly":
        return {data["source"]["source_id"]: {p["period"] for p in points}}, set()
    return {}, {str(p["document"]) for p in points if p.get("document")}


def _licence_rows(
    db: Session,
    root: Path,
    posture: str,
    sources: dict[str, Source],
    monthly: dict[str, set[str] | None],
    daily: set[str] | None,
) -> list[dict[str, Any]]:
    """One licence summary row per document the response read, per source (module docstring)."""
    out = []
    for source_id in sorted(set(monthly) & set(sources)):
        source = sources[source_id]
        for _period, retrieved in trends.monthly_retrievals(root, source_id, monthly[source_id]):
            out.append(licence_summary_row(source, source.licence, retrieved))
    if daily is not None and not daily:
        return out
    recorded = trends.daily_source_rows(root, posture, daily)
    ids = {row["source_id"] for row in recorded}
    current = {s.id: s for s in db.scalars(sa.select(Source).where(Source.id.in_(ids)))} if ids else {}
    for row in recorded:
        stored = current.get(row["source_id"])
        if stored is not None:
            out.append(licence_summary_row(stored, stored.licence, row["retrieved_at"]))
        else:
            out.append({k: v for k, v in row.items() if k != "document"})
    return out


@router.get("/v1/trends")
def get_trends(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    check_allowed(request, PARAMS)
    qp = request.query_params
    views, rows = _visible_sources(db, ctx.entitlement)
    root = trends.data_root()
    posture = platform_posture()
    meta = build_meta(tier=ctx.entitlement)
    metric = qp.get("metric")
    if not metric:
        for name in ("by", "from", "to"):
            if qp.get(name):
                raise validation_error(
                    name, f"{name} applies to one series: name it with metric", request.url.path
                )
        data: Any = trends.catalogue(root, views, posture)
    else:
        try:
            data = trends.series(
                root,
                views,
                posture,
                metric=metric,
                by=qp.get("by") or None,
                start=qp.get("from") or None,
                end=qp.get("to") or None,
            )
        except trends.SeriesError as exc:
            raise validation_error(exc.field, str(exc), request.url.path) from exc
    monthly, daily = _served(data, catalogue=not metric)
    summary = build_licence_summary(_licence_rows(db, root, posture, rows, monthly, daily))
    envelope = build_envelope(data, meta=meta, licence_summary=summary)
    envelope["notes"] = {
        "posture": posture,
        "like_for_like": (
            "Series from different sources are never joined into one line. A change is computed only between "
            "two points of the same series with the same basis (a daily basis includes the sources that "
            "contributed and how many were withheld; a monthly change also needs one parser to have read "
            "both months); a change of basis is listed under breaks."
        ),
    }
    return envelope
