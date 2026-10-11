"""Operator read of the site pass (docs/21 §3.25; lane S2): what recent rebuilds did to sites, and
which sites are flagged for review and so not served.

    GET /admin/v1/sites/review    recent `site_audit` rows, newest first, and every flagged site

Read-only. `site_audit` is append-only and operators only (no public route, feed, alert or webhook
reads it); this is its one reader. A flagged (`oversize`) site waits for an operator, but clearing
the flag needs a write path that does not exist yet: the builder sets the flag again on its next
pass while the site still groups more than `rules.OVERSIZE_MEMBERS` distinct things, so a clear
would need an override the builder honours. Until then this view lists flagged sites so they are
seen, and the fix is a rule or data change. Mounted with the public site routes
(`services/api/sites.py` includes this router), admin role required like every `/admin/v1` route;
the `admin.` host is the proxy's job (docs/20 §7).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.auth import AuthContext, require_admin
from services.api.deps import get_db
from services.api.pagination import clamp_limit
from services.api.params import check_allowed, int_param
from services.api.serialize import build_envelope, build_licence_summary, build_meta
from services.db.models import Site, SiteAudit

router = APIRouter()


def _site_row(site: Site) -> dict[str, Any]:
    return {
        "public_id": site.public_id,
        "name": site.name_display,
        "member_count": site.member_count,
        "review_flag": site.review_flag,
        "retired": site.retired_at is not None,
        "built_at": site.built_at.isoformat() if site.built_at else None,
        "rule_version": site.rule_version,
    }


@router.get("/admin/v1/sites/review")
def admin_site_review(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    check_allowed(request, {"limit"})
    limit = clamp_limit(int_param(request, "limit"))
    rows = db.execute(
        select(SiteAudit, Site)
        .join(Site, Site.id == SiteAudit.site_id)
        .order_by(SiteAudit.recorded_at.desc(), SiteAudit.id)
        .limit(limit)
    ).all()
    flagged = db.scalars(
        select(Site).where(Site.review_flag.is_not(None), Site.retired_at.is_(None)).order_by(Site.public_id)
    ).all()
    data = {
        "audit": [
            {
                "kind": audit.kind,
                "site": _site_row(site),
                "detail": audit.detail or {},
                "rule_version": audit.rule_version,
                "recorded_at": audit.recorded_at.isoformat(),
            }
            for audit, site in rows
        ],
        "flagged": [_site_row(site) for site in flagged],
    }
    return build_envelope(
        data, meta=build_meta(lag_days=0, tier="admin"), licence_summary=build_licence_summary([])
    )


__all__ = ["router"]
