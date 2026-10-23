"""The private-projects API: "My pipeline" (docs/19 §6.1; ADR 0009). Every operation is under
`/v1/my/`, session only, and reaches private rows through `services.projects.authz` alone.

**One gate, deny by default (docs/19 §5.1).** Every route under `/v1/my/projects/{project_id}`
takes `Depends(project_access(action))`; every other route takes `Depends(signed_in_user)`, which
is `authz.require_workspace_user`. An outsider, an API key, a removed member and a malformed id
all get the one `404` an unknown id gets; a member whose role is too low gets `403 forbidden_role`.
Writes take the project from the `ProjectAccess` the gate returned, never from a request body, and
a body key the operation does not take is refused, so no body can name an owner, a member or a
project id. The routes that take the dependency for `read` (PATCH of the project, removing a
member) do so because the action depends on the body (archive is owner-only, leaving is
self-service); each mutation in `services.projects` re-checks its own action
(`authz.require_action`).

**Private responses never leave the private path.**

- `PrivateHeadersMiddleware` puts `Cache-Control: private, no-store`, `X-Robots-Tag: noindex,
  nofollow`, `Referrer-Policy: no-referrer` and `Vary: Cookie` on every `/v1/my/` response, errors
  included, and drops any `ETag` (docs/19 §5.2 item 6). A `500` is sent from outside every
  middleware, so `private_unhandled_exception_handler` puts the same headers on it.
- `private_request_guard` (on the whole router) keeps every private response out of
  `idempotency_record`: that table is not private, is not erased with the account, and would hold
  an invitation link in clear for a day (docs/19 §2.6 rule 2). An `Idempotency-Key` is accepted
  and never replayed here; the writes that a retry could repeat are safe to repeat (a link or an
  evidence decision is idempotent; a second invitation replaces the first).
- The same guard refuses a state-changing request without an `Origin` naming the site (the site's
  own server-side calls, which carry its service identity, have done their own same-origin check),
  and one whose body is not JSON (docs/19 §5.1, "cross-site requests").
- Nothing here writes `event`, the admin audit log, `ui_event` or a log line carrying a value
  (docs/19 §4.1, §5.3): a project's history is `private_project_activity`, ids only.

**Linked records are read live (docs/19 §2.2, §4.2).** `resolve_link` re-reads each public record
through the read path and visibility predicate its public detail route uses, at the viewer's own
tier, follows a merge (or a retired site) to its visible survivor, and answers `unavailable` with
nothing else when the viewer can no longer see it. Nothing about a linked record is stored but its
type and public id.

**Prefill copies only what may be copied (docs/19 §7).** Starting a deal from a record fills the
project's own fields only from values whose sources are all `open` or `attribution`
(`GatedRecord.field_sources`); a field from any other class is left blank and named in
`prefill.left_blank`, and the live value stays visible on the linked record.

**Suggested evidence is rebuilt live, never verified (docs/19 §2.4).** `live_firings` reads each
linked proposal's served view and its visible source links and fires the five rules of the table;
only the reference and the rule are stored (`checklist.record_suggestion`), and every suggestion
carries the label "Suggested from public records · not verified".

**One tracker of projects, bids and investments** (decision row 2026-10-23; docs/30 §11.4). Every
item has a kind and a stage of that kind's (`services.projects.kinds`). The list is sorted by next
deadline, soonest first, then by the most recent change; `TrackerColumns` reads its two live columns:
the next deadline, the `due_at` of a linked RFP or funding notice through the gated serialiser (a
project also takes its open bids' deadlines), and "changed since your last visit", from the public
change events on the linked records that the viewer's tier may see, newer than the member's last
visit. Opening an item (`GET /v1/my/projects/{project_id}`) stamps that visit. "Track" from a
record (`from`) makes a bid from an RFP or funding notice, a project or an investment from a
project page, and a project from a site, a plant or a grid connection point (`kinds.TRACK_KINDS`).

**Handed to lane P4:** `export_user_data` (docs/19 §5.5) is the body of `GET /v1/my/export`; the
route around it (session, headers, download, the daily limit) is here.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import uuid as _uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Any, Final
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from services.api.auth import AuthContext, get_auth_context
from services.api.client_ip import is_internal_request
from services.api.common import WEB_HOST, iso
from services.api.deps import get_db
from services.api.errors import ProblemError, unknown_parameter
from services.api.geo import effective_placement
from services.api.idempotency import STATE_KEY as IDEMPOTENCY_STATE_KEY
from services.api.interconnection_points import (
    PointTotals,
    point_name,
    point_totals,
    serialize_point,
    visible_point,
)
from services.api.merged_redirect import merged_survivor
from services.api.params import check_allowed
from services.api.ratelimit import default_limiter
from services.api.request_context import unhandled_exception_handler
from services.api.serialize import (
    asset_geometry,
    build_envelope,
    build_licence_summary,
    build_meta,
    licence_summary_row,
    provenance_quartet,
    provenance_row,
    serialize_asset,
    serialize_opportunity,
    serialize_organization,
    serialize_proposal,
)
from services.api.sites import served_site, site_url
from services.api.visibility import (
    GatedRecord,
    asset_event_visibility_filter,
    asset_visibility_filter,
    event_visibility_filter,
    gated_record,
    interconnection_point_visible,
    opportunity_visibility_filter,
    proposal_visibility_filter,
    visible_organization_or_404,
    visible_source_links,
)
from services.api.withheld_names import withheld_names
from services.db.models import (
    Asset,
    Event,
    InterconnectionPoint,
    Opportunity,
    Proposal,
    ProposalSource,
    Site,
    Source,
    User,
)
from services.projects import authz, checklist, invitations
from services.projects import kinds as item_kinds
from services.projects import models as pm
from services.sites.switch import sites_enabled

#: Every private API path starts here (docs/19 §5.2 item 5): one rule covers caching, robots,
#: logs and error reports.
PRIVATE_API_PREFIX: Final = "/v1/my"
#: docs/19 §5.2 item 6, on every `/v1/my/` response.
PRIVATE_RESPONSE_HEADERS: Final[Mapping[str, str]] = {
    "Cache-Control": "private, no-store",
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
}
#: What a linked record the viewer can no longer see says, and all it says (docs/19 §2.2).
UNAVAILABLE_TEXT: Final = "This record is no longer available"
#: The prefill licence rule (docs/19 §7): the only reuse classes a project's own fields may be
#: copied from. Never `noncommercial`, whatever the platform posture.
PREFILL_REUSE_CLASSES: Final = frozenset({"open", "attribution"})
#: Burst limit on new invitations, per user, on top of P1's 20 a day (docs/19 §2.6 rule 6).
INVITATIONS_PER_HOUR: Final = 10
#: Preview and accept attempts, per user: a token is unguessable, but probing costs nothing else.
INVITATION_ATTEMPTS_PER_HOUR: Final = 30
#: docs/19 §5.5: at most 5 exports a day.
EXPORTS_PER_DAY: Final = 5
ACTIVITY_DEFAULT_LIMIT: Final = 100
ACTIVITY_MAX_LIMIT: Final = 200
FORMER_MEMBER: Final = "Former member"
#: Lifecycle states at or after a queue filing (docs/19 §2.4, "lifecycle `filed` or later").
FILED_OR_LATER: Final = frozenset(
    {"filed", "studied", "permitted", "contracted", "under_construction", "built"}
)
#: Starting records `Start a deal` is not offered on (docs/19 §1).
CLOSED_PROPOSAL_STATES: Final = frozenset({"withdrawn", "cancelled"})
OPEN_OPPORTUNITY_STATUSES: Final = frozenset({"announced", "open", "reinstated"})
STARTABLE_ASSET_STATUSES: Final = frozenset({"retiring", "retired"})
#: Opportunity kinds that are funding rather than an RFP (docs/19 §1: `funding` for funding notices).
FUNDING_KINDS: Final = frozenset({"foa", "loan_program", "program"})

#: docs/19 §2.4 rule sources, by `data/sources.yaml` id. `services/api/test_my_projects.py` pins
#: that each id exists in the registry.
TEC_REGISTER_SOURCE: Final = "gb.neso.tec_register"
AIR_PERMIT_SOURCES: Final = frozenset(
    {
        "us.tx.tceq.air_permits",
        "us.va.deq.data_center_air_sites",
        "us.epa.echo.icis_air",
        "us.ga.epd.air_permit_advisories",
        "us.epa.rblc",
    }
)
#: "A federal permitting or NEPA record, or a FERC docket".
FEDERAL_RECORD_SOURCES: Final = frozenset(
    {
        "us.permits_dashboard",
        "us.blm.eplanning",
        "us.epa.eis_database",
        "us.boem.renewables",
        "us.usace.orm_public",
        "us.ferc.elibrary",
        "us.ferc.pipelines_pending",
    }
)

#: docs/19 §1: the relations a deal may be started with, per starting record, default first. A site
#: is a candidate site for the project tracked from it. An investment is in the project itself.
START_RELATIONS: Final[Mapping[str, tuple[str, ...]]] = {
    "proposal": ("this_project", "nearby"),
    "site": ("candidate_site", "nearby"),
    "asset": ("grid_connection",),
    "opportunity": ("offtake_or_rfp", "funding"),
    "interconnection_point": ("grid_connection",),
}
INVESTMENT_RELATIONS: Final = ("this_project",)
#: A proposal's lifecycle as a suggested stage (docs/19 §1 "a suggested stage"), for `this_project`.
STAGE_FROM_LIFECYCLE: Final[Mapping[str, str]] = {
    "unknown": "prospecting",
    "announced": "prospecting",
    "filed": "development",
    "studied": "development",
    "permitted": "development",
    "contracted": "ready_to_build",
    "under_construction": "construction",
    "built": "operating",
}
#: An opportunity's technology tokens (`pipeline.connectors.opportunity.TECH_KEYWORDS`) in the
#: project vocabulary (`services.projects.models.PROJECT_TECHNOLOGIES`); others have no equivalent.
OPPORTUNITY_TECHNOLOGY_MAP: Final[Mapping[str, str]] = {
    "wind_offshore": "wind_offshore",
    "wind": "wind",
    "solar_pv": "solar",
    "bess": "storage",
    "pumped_storage": "pumped_storage",
    "hydro": "hydro",
    "marine": "marine",
    "nuclear": "nuclear",
    "hydrogen": "hydrogen",
    "geothermal": "geothermal",
    "biomass": "biomass",
    "gas": "gas_other",
    "transmission": "transmission",
}
#: The item fields a body may set; `point_precision` is derived (a pin moved by hand is `user`; a
#: prefilled point keeps its record's grade), never taken from a body. `kind` is taken at creation
#: only (an edit naming it is refused with the reason).
BODY_FIELDS: Final = tuple(name for name in pm.PROJECT_FIELDS if name != "point_precision")
#: The member's own preference, which an edit body may also carry (any role may set their own).
MEMBER_FIELDS: Final = ("email_changes",)
PREFILL_POINT_PRECISIONS: Final = frozenset({"exact", "county_centroid", "state_centroid"})
_STATE_CHANGING: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SAFE_KEY = re.compile(r"[a-z][a-z0-9_.]{0,47}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ASCII_COUNT = re.compile(r"[0-9]{1,4}")


# ======================================================================== headers and the guard
def is_private_path(path: str) -> bool:
    """`/v1/my` or anything under it."""
    return path == PRIVATE_API_PREFIX or path.startswith(PRIVATE_API_PREFIX + "/")


def _merged_vary(existing: str | None, name: str) -> str:
    tokens = [t.strip() for t in (existing or "").split(",") if t.strip()]
    if name.lower() not in {t.lower() for t in tokens}:
        tokens.append(name)
    return ", ".join(tokens)


def apply_private_headers(headers: MutableHeaders) -> None:
    """What every `/v1/my/` response carries (docs/19 §5.2 item 6), `Vary: Cookie`, and no entity
    tag. One definition for the middleware and the `500` handler."""
    for name, value in PRIVATE_RESPONSE_HEADERS.items():
        headers[name] = value
    headers["Vary"] = _merged_vary(headers.get("vary"), "Cookie")
    if "etag" in headers:
        del headers["etag"]


class PrivateHeadersMiddleware:
    """Pure ASGI, mounted outermost among the app's middleware (`services/api/app.py`), so it has
    the last word on every `/v1/my/` response's headers. A `500` is the exception: Starlette's
    `ServerErrorMiddleware` sends it from outside every user middleware, so
    `private_unhandled_exception_handler` sets the same headers on it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_private_path(str(scope.get("path", ""))):
            await self.app(scope, receive, send)
            return

        async def send_private(message: Message) -> None:
            if message["type"] == "http.response.start":
                apply_private_headers(MutableHeaders(scope=message))
            await send(message)

        await self.app(scope, receive, send_private)


async def private_unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    """`request_context.unhandled_exception_handler` (the fixed `500` problem with the request id),
    plus the private headers when the path is private. Registered in `services/api/app.py` for
    `Exception`. Before 2026-10-23 a `/v1/my/` 500 went out with no `no-store`, `noindex` or
    `no-referrer`, and the proxy then added its own `Referrer-Policy` (lane P2 security review)."""
    response = await unhandled_exception_handler(request, exc)
    if is_private_path(request.url.path):
        apply_private_headers(response.headers)
    return response


def _invalid(field_name: str, message: str) -> ProblemError:
    return ProblemError(
        "validation_error",
        "Invalid request",
        detail=message,
        errors=[{"field": field_name, "message": message}],
    )


def _names_this_site(origin: str, request: Request) -> bool:
    parsed = urlsplit(origin)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    site = urlsplit(WEB_HOST)
    if (parsed.scheme, parsed.netloc.lower()) == (site.scheme, site.netloc.lower()):
        return True
    # The host this request was addressed to: a browser sets `Host` to the target and `Origin` to
    # the page it runs on, so a page elsewhere cannot make them agree. The scheme must agree too, so
    # a page served over plain `http://` on this host is not this site. Behind the proxy the request's
    # scheme is the visitor's: uvicorn believes Caddy's `X-Forwarded-Proto` (`infra/entrypoint.py`).
    return (parsed.scheme, parsed.netloc.lower()) == (request.url.scheme, request.url.netloc.lower())


def _has_body(request: Request) -> bool:
    length = request.headers.get("content-length")
    if length is not None:
        return length.strip() not in ("", "0")
    return "transfer-encoding" in request.headers


#: The workspace's launch switch (docs/19 §8 go-live gate): every /v1/my route answers 404 until the
#: deployment sets `MY_PIPELINE=on`, so the API can ship before the pages, the privacy notice and the
#: owner's go-live decision. Read per request, so turning it on needs no restart of the test process.
MY_PIPELINE_ENV = "MY_PIPELINE"


def workspace_enabled() -> bool:
    return os.environ.get(MY_PIPELINE_ENV, "").strip().lower() == "on"


def private_request_guard(request: Request) -> None:
    """On every private route (module docstring): the launch switch is on, no private response is
    stored for idempotent replay, and a state-changing request must come from the site and carry
    JSON."""
    if not workspace_enabled():
        raise authz.project_not_found()
    state = request.scope.get("state")
    if isinstance(state, dict):
        state.pop(IDEMPOTENCY_STATE_KEY, None)
    if request.method not in _STATE_CHANGING:
        return
    for name in request.query_params:
        # A write takes its input from the body only; a query string would reach access logs.
        raise unknown_parameter(_body_key(name), request.url.path)
    if not is_internal_request(request):
        origin = request.headers.get("origin")
        if not origin or not _names_this_site(origin, request):
            raise ProblemError(
                "validation_error",
                "Cross-site request refused",
                detail="A change to a private project must come from this site (an Origin header naming it).",
                errors=[{"field": "Origin", "message": "must name this site"}],
            )
    if _has_body(request):
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise _invalid("Content-Type", "the body must be application/json")


router = APIRouter(dependencies=[Depends(private_request_guard)])


def signed_in_user(ctx: Annotated[AuthContext, Depends(get_auth_context)]) -> User:
    """The routes that name no project (list, create, pins, prefill, export, invitations): a
    session for an active user with a verified email (`authz.require_workspace_user`)."""
    return authz.require_workspace_user(ctx)


# ============================================================================ small serialisers
def _float(value: Decimal | float | int | None) -> float | None:
    return float(value) if value is not None else None


def _when(value: dt.datetime | None) -> str | None:
    return iso(pm.as_utc(value)) if value is not None else None


def _envelope(
    ctx: AuthContext, data: Any, licence_rows: Iterable[Mapping[str, Any]] = (), **meta: Any
) -> dict[str, Any]:
    return build_envelope(
        data,
        meta=build_meta(tier=ctx.entitlement, extra=meta or None),
        licence_summary=build_licence_summary([dict(row) for row in licence_rows]),
    )


def _quartet(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "source_id",
        "source_name",
        "source_url",
        "retrieved_at",
        "licence_id",
        "reuse_class",
        "attribution_text",
    )
    return {key: row.get(key) for key in keys}


def _project_url(public_id: str) -> str:
    """An item's private page (docs/30 §11.10: `/my/items/{id}`)."""
    return f"{WEB_HOST}/my/items/{public_id}"


def _progress_json(done: int, total: int) -> dict[str, Any]:
    return {"done": done, "total": total, "label": f"{done} of {total} done, your status"}


def _fit_title(text: str | None) -> str | None:
    if not text:
        return None
    clean = " ".join(_CONTROL.sub(" ", text).split())
    if not clean:
        return None
    if len(clean) > pm.TITLE_MAX_CHARS:
        clean = clean[: pm.TITLE_MAX_CHARS - 1].rstrip() + "…"
    return clean


def _body_key(name: object) -> str:
    """A body key as an error may name it: an identifier-shaped key, else "body", so a value a
    caller typed into a key is never echoed."""
    return name if isinstance(name, str) and _SAFE_KEY.fullmatch(name) else "body"


def _only(body: Mapping[str, Any], allowed: Iterable[str], *, prefix: str = "") -> None:
    allowed_set = set(allowed)
    for key in body:
        if key not in allowed_set:
            name = _body_key(key)
            raise _invalid(f"{prefix}{name}" if name != "body" else "body", f"{name} cannot be set here")


def _text(body: Mapping[str, Any], key: str, *, field_name: str | None = None) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise _invalid(field_name or key, f"{field_name or key} is required")
    return value


def _uuid_or_404(value: str) -> _uuid.UUID:
    try:
        return _uuid.UUID(value)
    except ValueError:
        raise authz.project_not_found() from None


def _limit(key: str, limit: int, window_seconds: int, what: str) -> None:
    result = default_limiter.check(key, limit=limit, window_seconds=window_seconds)
    if not result.allowed:
        raise ProblemError(
            "rate_limited",
            "Rate limit exceeded",
            detail=f"More than {limit} {what} in the current window.",
            headers={"Retry-After": str(result.reset_seconds)},
        )


# ================================================================================ live links
@dataclass(frozen=True)
class Resolution:
    """One of the three outcomes docs/19 §4.2 names, for one reference: `available` (the record,
    read live), `merged` (the record was merged, or the site retired; the summary is the visible
    survivor's) or `unavailable` (nothing else is said). `record` is the served object for this
    module's own use (evidence, prefill) and is never serialised."""

    state: str
    record_type: str
    public_id: str
    summary: dict[str, Any] | None = None
    survivor_public_id: str | None = None
    licence_rows: tuple[dict[str, Any], ...] = ()
    record: Any = field(default=None, repr=False, compare=False)

    @property
    def visible(self) -> bool:
        return self.state != "unavailable"


def _unavailable(record_type: str, public_id: str) -> Resolution:
    return Resolution("unavailable", record_type, public_id)


def _resolved(
    record_type: str,
    stored: str,
    live_public_id: str,
    summary: dict[str, Any],
    licence_rows: Iterable[dict[str, Any]],
    record: Any,
) -> Resolution:
    moved = live_public_id != stored
    return Resolution(
        "merged" if moved else "available",
        record_type,
        stored,
        summary=summary,
        survivor_public_id=live_public_id if moved else None,
        licence_rows=tuple(licence_rows),
        record=record,
    )


def _record_licence_rows(record: Proposal | Opportunity, entitlement: str) -> list[dict[str, Any]]:
    return [
        licence_summary_row(link.source, link.source.licence, link.retrieved_at)
        for link in visible_source_links(record.sources, entitlement)
    ]


def _resolve_proposal(db: Session, public_id: str, entitlement: str) -> Resolution:
    prop = db.scalar(
        select(Proposal).where(Proposal.public_id == public_id, *proposal_visibility_filter(entitlement))
    )
    if prop is None:
        survivor = merged_survivor(db, Proposal, public_id, proposal_visibility_filter, entitlement)
        if not isinstance(survivor, Proposal):
            return _unavailable("proposal", public_id)
        prop = survivor
    data = serialize_proposal(prop, entitlement=entitlement)
    summary = {
        "record_type": "proposal",
        "public_id": data["public_id"],
        "url": data["url"],
        "name": data["name_canonical"],
        "status": data["lifecycle_state"],
        "facts": {
            "kind": data["kind"],
            "technology": data["technology"],
            "capacity_mw": data["capacity_mw"],
            "storage_mwh": data["storage_mwh"],
            "jurisdiction": data["jurisdiction"],
            "iso": data["iso"],
            "proposed_online_date": data["proposed_online_date"],
            "listed": data["listed"],
            "sponsor": (data["sponsor"] or {}).get("name_canonical"),
        },
        "sources": [_quartet(row) for row in data["provenance"]],
    }
    rows = _record_licence_rows(prop, entitlement)
    return _resolved("proposal", public_id, prop.public_id, summary, rows, gated_record(prop, entitlement))


def _resolve_opportunity(db: Session, public_id: str, entitlement: str) -> Resolution:
    opp = db.scalar(
        select(Opportunity).where(
            Opportunity.public_id == public_id, *opportunity_visibility_filter(entitlement)
        )
    )
    if opp is None:
        survivor = merged_survivor(db, Opportunity, public_id, opportunity_visibility_filter, entitlement)
        if not isinstance(survivor, Opportunity):
            return _unavailable("opportunity", public_id)
        opp = survivor
    data = serialize_opportunity(opp, entitlement=entitlement)
    summary = {
        "record_type": "opportunity",
        "public_id": data["public_id"],
        "url": data["url"],
        "name": data["title"],
        "status": data["status"],
        "facts": {
            "kind": data["kind"],
            "technologies": data["technologies"],
            "capacity_sought_mw": data["capacity_sought_mw"],
            "budget_amount": data["budget_amount"],
            "budget_currency": data["budget_currency"],
            "open_at": data["open_at"],
            "due_at": data["due_at"],
            "jurisdiction": data["jurisdiction"],
            "issuer": (data["issuer"] or {}).get("name_canonical"),
        },
        "sources": [_quartet(row) for row in data["provenance"]],
    }
    rows = _record_licence_rows(opp, entitlement)
    return _resolved("opportunity", public_id, opp.public_id, summary, rows, gated_record(opp, entitlement))


def _resolve_asset(db: Session, public_id: str, entitlement: str) -> Resolution:
    # `GET /v1/assets/{public_id}` reads the public tier for every caller; so does this.
    del entitlement
    asset = db.scalar(select(Asset).where(Asset.public_id == public_id, *asset_visibility_filter()))
    if asset is None:
        return _unavailable("asset", public_id)
    data = serialize_asset(asset, withheld=withheld_names(db), include_owners=False, include_geometry=False)
    summary = {
        "record_type": "asset",
        "public_id": data["public_id"],
        "url": data["url"],
        "name": data["name"],
        "status": data["status"],
        "facts": {
            "asset_type": data["asset_type"],
            "technology": data["technology"],
            "capacity_mw": data["capacity_mw"],
            "commissioned_year": data["commissioned_year"],
            "retirement_year": data["retirement_year"],
            "operator_name": data["operator_name"],
            "state_code": data["state_code"],
            "county_name": data["county_name"],
        },
        "sources": [_quartet(row) for row in data["provenance"]],
    }
    rows = [licence_summary_row(asset.source, asset.licence, asset.retrieved_at)]
    return _resolved("asset", public_id, asset.public_id, summary, rows, asset)


def _resolve_organization(db: Session, public_id: str, entitlement: str) -> Resolution:
    del entitlement  # the organisation detail route reads the public tier for every caller
    try:
        org = visible_organization_or_404(db, public_id, PRIVATE_API_PREFIX)
    except ProblemError:
        return _unavailable("organization", public_id)
    data = serialize_organization(org)
    summary = {
        "record_type": "organization",
        "public_id": data["public_id"],
        "url": data["url"],
        "name": data["name_canonical"],
        "status": None,
        "facts": {"type": data["type"], "country": data["country"], "jurisdiction": data["jurisdiction"]},
        # As on its public page: an organisation is a derived entity with no row of its own to credit.
        "sources": [],
    }
    return _resolved("organization", public_id, org.public_id, summary, (), org)


def _resolve_point(db: Session, public_id: str, entitlement: str) -> Resolution:
    point = visible_point(db, public_id, entitlement)
    if point is None:
        return _unavailable("interconnection_point", public_id)
    totals = point_totals(db, [point.id], entitlement).get(point.id, PointTotals())
    data = serialize_point(point, totals, entitlement)
    summary = {
        "record_type": "interconnection_point",
        "public_id": data["public_id"],
        "url": data["url"],
        "name": data["name"],
        "status": None,
        "facts": {
            "iso": data["iso"],
            "kind": data["kind"],
            "voltage_kv": data["voltage_kv"],
            "jurisdiction": data["jurisdiction"],
            "proposal_count": data["totals"]["proposal_count"],
            "active_mw": data["totals"]["active_mw"],
        },
        "sources": [_quartet(row) for row in data["provenance"]],
    }
    rows = [licence_summary_row(point.source, point.licence, point.retrieved_at)]
    return _resolved("interconnection_point", public_id, point.public_id, summary, rows, point)


def _live_site(db: Session, site: Site) -> Site | None:
    """A retired site's successor chain, as `GET /v1/sites/{public_id}` follows it."""
    seen = {site.id}
    current = site
    for _ in range(10):
        if current.retired_at is None:
            return current
        nxt = db.get(Site, current.successor_site_id) if current.successor_site_id else None
        if nxt is None or nxt.id in seen:
            return None
        seen.add(nxt.id)
        current = nxt
    return None


def _resolve_site(db: Session, public_id: str, entitlement: str) -> Resolution:
    stored = db.scalar(select(Site).where(Site.public_id == public_id)) if sites_enabled() else None
    site = _live_site(db, stored) if stored is not None else None
    served = served_site(db, site, entitlement) if site is not None else None
    if site is None or served is None:
        return _unavailable("site", public_id)
    lead = gated_record(served.lead.proposal, entitlement)
    credits: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for member in served.members:
        for link in visible_source_links(member.proposal.sources, entitlement):
            rows.append(licence_summary_row(link.source, link.source.licence, link.retrieved_at))
            quartet = _quartet(provenance_row(link, link.source))
            quartet["source_url"] = link.source.url
            known = credits.get(link.source_id)
            if known is None or str(quartet["retrieved_at"]) > str(known["retrieved_at"]):
                credits[link.source_id] = quartet
    summary = {
        "record_type": "site",
        "public_id": site.public_id,
        "url": site_url(site),
        "name": lead.name_canonical,
        "status": None,
        "facts": {"member_count": len(served.members), "partial": served.partial},
        "sources": [credits[key] for key in sorted(credits)],
    }
    return _resolved("site", public_id, site.public_id, summary, rows, served)


_RESOLVERS: Final[Mapping[str, Callable[[Session, str, str], Resolution]]] = {
    "proposal": _resolve_proposal,
    "opportunity": _resolve_opportunity,
    "asset": _resolve_asset,
    "organization": _resolve_organization,
    "interconnection_point": _resolve_point,
    "site": _resolve_site,
}


def resolve_reference(db: Session, record_type: str, public_id: str, entitlement: str) -> Resolution:
    """`record_type`/`public_id` read live at `entitlement` (docs/19 §4.2). A malformed reference,
    and a corridor (lane P6 settles how corridors are addressed), are `unavailable`."""
    resolver = _RESOLVERS.get(record_type)
    if resolver is None or not pm.is_record_public_id(record_type, public_id):
        return _unavailable(record_type, public_id)
    return resolver(db, public_id, entitlement)


def resolve_link(db: Session, link: pm.PrivateProjectLink, entitlement: str) -> Resolution:
    """One stored link, read live through its record's public read path (docs/19 §4.2)."""
    if link.record_public_id is None:
        return _unavailable(link.record_type, "")
    return resolve_reference(db, link.record_type, link.record_public_id, entitlement)


def _project_links(db: Session, access: authz.ProjectAccess) -> list[pm.PrivateProjectLink]:
    authz.require_action(access, "read")
    return list(
        db.execute(
            select(pm.PrivateProjectLink)
            .where(pm.PrivateProjectLink.project_id == access.project.id)
            .order_by(pm.PrivateProjectLink.created_at, pm.PrivateProjectLink.id)
        ).scalars()
    )


def _link_json(link: pm.PrivateProjectLink, resolution: Resolution) -> dict[str, Any]:
    """A linked record the viewer cannot see now shows its type and the message, and no id: the
    id of a paid-only record told a free member it exists, and a uuid7 id says when it was made
    (docs/19 §2.2, "nothing else"; lane P2 security review, 2026-10-23)."""
    return {
        "link_id": str(link.id),
        "record_type": link.record_type,
        "public_id": link.record_public_id if resolution.visible else None,
        "relation": link.relation,
        "added_at": _when(link.created_at),
        "state": resolution.state,
        "survivor_public_id": resolution.survivor_public_id,
        "record": resolution.summary if resolution.visible else None,
        "message": None if resolution.visible else UNAVAILABLE_TEXT,
    }


def _resolve_all(
    db: Session, links: Sequence[pm.PrivateProjectLink], entitlement: str
) -> list[tuple[pm.PrivateProjectLink, Resolution]]:
    return [(link, resolve_link(db, link, entitlement)) for link in links]


class _Sight:
    """Whether the viewer can see a public record now, each record read once per request through
    `resolve_reference`. A project's origin and its history show the id of a record only to a
    viewer who can see it; otherwise they show its type alone, as a link does (`_link_json`)."""

    def __init__(self, db: Session, entitlement: str, known: Iterable[Resolution] = ()) -> None:
        self._db = db
        self._entitlement = entitlement
        self._seen: dict[tuple[str, str], bool] = {(r.record_type, r.public_id): r.visible for r in known}

    def shown_id(self, record_type: object, public_id: object) -> str | None:
        if not isinstance(record_type, str) or not isinstance(public_id, str):
            return None
        key = (record_type, public_id)
        if key not in self._seen:
            self._seen[key] = resolve_reference(self._db, record_type, public_id, self._entitlement).visible
        return public_id if self._seen[key] else None


# =================================================================================== prefill
@dataclass(frozen=True)
class Prefill:
    """The project's own fields from a starting record, under the licence rule (docs/19 §7)."""

    record_type: str
    public_id: str
    relations: tuple[str, ...]
    values: dict[str, object]
    left_blank: tuple[str, ...]
    resolution: Resolution
    #: The kind of item it makes, and the kinds this record may make (`kinds.TRACK_KINDS`).
    kind: str = "project"
    kinds: tuple[str, ...] = ("project",)

    @property
    def default_relation(self) -> str:
        return self.relations[0]


class _Supply:
    """Which of a served record's fields may be copied: every source that supplied the served
    value is `open` or `attribution`. A field nobody supplied (an operator override, a value no
    readable source states) fails closed."""

    def __init__(self, db: Session, view: Any) -> None:
        self._db = db
        self._sources: dict[str, dict[str, Any]] = (
            view.field_sources() if isinstance(view, GatedRecord) else {}
        )

    def copyable(self, name: str) -> bool:
        ids = (self._sources.get(name) or {}).get("source_ids") or []
        if not ids:
            return False
        for source_id in ids:
            source = self._db.get(Source, source_id)
            if source is None or source.licence.reuse_class not in PREFILL_REUSE_CLASSES:
                return False
        return True


def _copyable_licences(*licences: Any) -> bool:
    return all(lic is not None and lic.reuse_class in PREFILL_REUSE_CLASSES for lic in licences)


def _offer(values: dict[str, object], blank: list[str], name: str, value: object, allowed: bool) -> None:
    if value is None:
        return
    if allowed:
        values[name] = value
    else:
        blank.append(name)


def _proposal_prefill(
    db: Session, view: Any, relation: str, kind: str = "project"
) -> tuple[dict[str, object], list[str]]:
    supply = _Supply(db, view)
    values: dict[str, object] = {}
    blank: list[str] = []
    _offer(values, blank, "title", _fit_title(view.name_canonical), supply.copyable("name_canonical"))
    technology = view.technology if view.technology in pm.PROJECT_TECHNOLOGIES else None
    _offer(values, blank, "technology", technology, supply.copyable("technology"))
    _offer(values, blank, "capacity_mw", _float(view.capacity_mw), supply.copyable("capacity_mw"))
    _offer(values, blank, "storage_mwh", _float(view.storage_mwh), supply.copyable("storage_mwh"))
    if relation == "this_project" and kind == "project":
        stage = STAGE_FROM_LIFECYCLE.get(view.lifecycle_state)
        _offer(values, blank, "stage", stage, supply.copyable("lifecycle_state"))
    location = view.location
    if location is not None:
        placement = effective_placement(location)
        if placement.geom is not None and placement.precision in PREFILL_POINT_PRECISIONS:
            lon, lat = placement.geom
            if _copyable_licences(location.licence, location.source.licence):
                values.update(lat=float(lat), lon=float(lon), point_precision=placement.precision)
            else:
                blank.extend(["lat", "lon"])
    return values, blank


def _opportunity_prefill(db: Session, view: Any) -> tuple[dict[str, object], list[str]]:
    supply = _Supply(db, view)
    values: dict[str, object] = {}
    blank: list[str] = []
    _offer(
        values,
        blank,
        "title",
        _fit_title(f"Bid: {view.title}" if view.title else None),
        supply.copyable("title"),
    )
    mapped = {OPPORTUNITY_TECHNOLOGY_MAP.get(token) for token in (view.technologies or [])}
    technology = mapped.pop() if len(mapped) == 1 else None
    _offer(values, blank, "technology", technology, supply.copyable("technologies"))
    capacity = _float(view.capacity_sought_mw)
    _offer(values, blank, "capacity_mw", capacity, supply.copyable("capacity_sought_mw"))
    return values, blank


def _asset_prefill(asset: Asset) -> tuple[dict[str, object], list[str]]:
    values: dict[str, object] = {}
    blank: list[str] = []
    allowed = _copyable_licences(asset.licence, asset.source.licence)
    _offer(values, blank, "title", _fit_title(f"New project at {asset.name}"), allowed)
    geometry = asset_geometry(asset)
    if geometry is not None and geometry.get("type") == "Point":
        lon, lat = geometry["coordinates"]
        if allowed:
            values.update(lat=float(lat), lon=float(lon), point_precision="exact")
        else:
            blank.extend(["lat", "lon"])
    return values, blank


def _site_prefill(db: Session, served: Any, entitlement: str) -> tuple[dict[str, object], list[str]]:
    """A project at a site: its title from the lead record's name and its map point, each under the
    licence rule; the lead's own technology and capacity are not the user's project's."""
    lead = gated_record(served.lead.proposal, entitlement)
    values, blank = _proposal_prefill(db, lead, "nearby")
    out = {key: value for key, value in values.items() if key in ("lat", "lon", "point_precision")}
    left = [name for name in blank if name in ("title", "lat", "lon")]
    if "title" in values:
        out["title"] = _fit_title(f"Project at {values['title']}")
    return out, left


def _point_prefill(point: InterconnectionPoint) -> tuple[dict[str, object], list[str]]:
    values: dict[str, object] = {}
    blank: list[str] = []
    voltage = f" ({float(point.voltage_kv):g} kV)" if point.voltage_kv is not None else ""
    allowed = _copyable_licences(point.licence, point.source.licence)
    _offer(values, blank, "title", _fit_title(f"Project at {point_name(point)}{voltage}"), allowed)
    return values, blank


def _startable(resolution: Resolution) -> bool:
    """docs/19 §1: which records offer "Start a deal"."""
    record = resolution.record
    if resolution.record_type == "proposal":
        return record.lifecycle_state not in CLOSED_PROPOSAL_STATES
    if resolution.record_type == "opportunity":
        return record.status in OPEN_OPPORTUNITY_STATUSES
    if resolution.record_type == "asset":
        return bool(record.asset_type == "power_plant" and record.status in STARTABLE_ASSET_STATUSES)
    return resolution.record_type in ("interconnection_point", "site")


def _start_relations(resolution: Resolution, kind: str = "project") -> tuple[str, ...]:
    relations = START_RELATIONS[resolution.record_type]
    if resolution.record_type == "proposal" and kind == "investment":
        return INVESTMENT_RELATIONS
    if resolution.record_type == "opportunity" and resolution.record.kind in FUNDING_KINDS:
        return ("funding", "offtake_or_rfp")
    return relations


def _track_kind(record_type: str, kind: object) -> str:
    """The kind of item "Track" makes from `record_type` (`kinds.TRACK_KINDS`): the one asked for,
    when that record may make it, else the record's default."""
    offered = item_kinds.TRACK_KINDS[record_type]
    if kind is None:
        return offered[0]
    if kind not in offered:
        raise _invalid("kind", f"tracking this record makes a {' or a '.join(offered)}")
    return str(kind)


def build_prefill(
    db: Session,
    record_type: object,
    public_id: object,
    relation: object,
    entitlement: str,
    kind: object = None,
) -> Prefill:
    """The starting record read live (refused with a `400` when the viewer cannot see it or docs/19
    §1 does not offer "Track" on it), the kind of item it makes and the fields it may prefill."""
    if not isinstance(record_type, str) or record_type not in START_RELATIONS:
        raise _invalid(
            "from.record_type",
            "an item is tracked from a proposal, site, asset, opportunity or interconnection point",
        )
    if not isinstance(public_id, str) or not pm.is_record_public_id(record_type, public_id):
        raise _invalid("from.public_id", "public_id is not an id of that record type")
    chosen = _track_kind(record_type, kind)
    resolution = resolve_reference(db, record_type, public_id, entitlement)
    if not resolution.visible:
        raise _invalid("from.public_id", "no such record is visible")
    if not _startable(resolution):
        raise _invalid("from.public_id", "Track is not offered on this record")
    relations = _start_relations(resolution, chosen)
    if relation is None:
        relation = relations[0]
    if relation not in relations:
        raise _invalid("from.relation", "relation is one of " + ", ".join(relations))
    record = resolution.record
    if record_type == "proposal":
        values, blank = _proposal_prefill(db, record, str(relation), chosen)
    elif record_type == "opportunity":
        values, blank = _opportunity_prefill(db, record)
    elif record_type == "asset":
        values, blank = _asset_prefill(record)
    elif record_type == "site":
        values, blank = _site_prefill(db, record, entitlement)
    else:
        values, blank = _point_prefill(record)
    ordered = (str(relation), *(r for r in relations if r != relation))
    live_id = resolution.survivor_public_id or public_id
    return Prefill(
        record_type,
        live_id,
        ordered,
        values,
        tuple(dict.fromkeys(blank)),
        resolution,
        kind=chosen,
        kinds=item_kinds.TRACK_KINDS[record_type],
    )


def _prefill_json(prefill: Prefill, *, applied: Iterable[str] | None = None) -> dict[str, Any]:
    applied_names = list(prefill.values) if applied is None else list(applied)
    shown = [name for name in applied_names if name != "point_precision"]
    return {
        "from": {"record_type": prefill.record_type, "public_id": prefill.public_id},
        "kind": prefill.kind,
        "kinds": list(prefill.kinds),
        "relation": prefill.default_relation,
        "relations": list(prefill.relations),
        "fields": [name for name in pm.PROJECT_FIELDS if name in shown],
        "left_blank": [{"field": name, "reason": "licence"} for name in prefill.left_blank],
    }


# ============================================================================ live evidence
@dataclass(frozen=True)
class Firing:
    """One docs/19 §2.4 rule firing on one linked record, rebuilt on every read. `stored_public_id`
    is the link's reference (what `record_suggestion` stores); `record` is the live record."""

    item_key: str
    rule: str
    record_type: str
    stored_public_id: str
    text: str
    source: dict[str, Any]
    record: dict[str, Any]

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.item_key, self.record_type, self.stored_public_id, self.rule)


def _status_words(view: Any) -> str:
    raw = view.status_raw
    if isinstance(raw, str) and raw.strip():
        return " ".join(raw.split())
    return str(view.lifecycle_state).replace("_", " ")


def _identifying(link: ProposalSource) -> bool:
    """Whether a source row's own id may be printed: the rule `serialize.provenance_row` applies
    to `source_record_id`."""
    licence = link.source.licence
    return bool(licence.allows_raw_publication or licence.reuse_class == "open")


def _queue_id(view: Any, link: ProposalSource) -> str | None:
    if not _identifying(link):
        return None
    queue_ids = (view.identifiers or {}).get("queue_ids") or []
    iso_code = (link.normalised or {}).get("iso")
    for entry in queue_ids:
        if isinstance(entry, dict) and entry.get("id") and (not iso_code or entry.get("iso") == iso_code):
            return str(entry["id"])
    return link.source_record_id or None


def _date_text(value: object) -> str | None:
    text = str(value)[:10] if value is not None else ""
    return text if _ISO_DATE.fullmatch(text) else None


def _proposal_firings(view: Any, stored_public_id: str, entitlement: str) -> list[Firing]:
    record = {
        "record_type": "proposal",
        "public_id": view.public_id,
        "url": f"{WEB_HOST}/proposals/{view.slug}",
        "name": view.name_canonical,
    }
    out: list[Firing] = []

    def fire(rule: str, link: ProposalSource, text: str) -> None:
        out.append(
            Firing(
                item_key=checklist.EVIDENCE_RULE_ITEMS[rule],
                rule=rule,
                record_type="proposal",
                stored_public_id=stored_public_id,
                text=text,
                source=_quartet(provenance_row(link, link.source)),
                record=record,
            )
        )

    for link in view.sources:
        source = link.source
        normalised = link.normalised or {}
        agency = source.operator or source.name
        if source.id == TEC_REGISTER_SOURCE:
            capacity = (
                f"{float(view.capacity_mw):g} MW" if view.capacity_mw is not None else "capacity not stated"
            )
            point = getattr(view, "interconnection_point", None)
            site = (
                point_name(point)
                if point is not None and interconnection_point_visible(point, entitlement)
                else view.name_canonical
            )
            when = _date_text(view.proposed_online_date) or "not stated"
            fire(
                "gb_tec_register",
                link,
                f"NESO TEC register lists a connection agreement: {capacity} at {site}, "
                f"connection date {when}.",
            )
        elif source.category == "generation_queue":
            if view.lifecycle_state in FILED_OR_LATER:
                queue_id = _queue_id(view, link)
                named = f"{agency} queue {queue_id}" if queue_id else f"{agency} queue record"
                fire(
                    "queue_position",
                    link,
                    f"{named}, status as stated: {_status_words(view)}. "
                    "A queue record, not an interconnection agreement.",
                )
            signed = _date_text(normalised.get("ia_date"))
            if signed is not None:
                fire(
                    "executed_interconnection_agreement",
                    link,
                    f"{source.name} reports an executed interconnection agreement (as of {signed}). "
                    "Your copy of the agreement is the evidence.",
                )
        elif source.id in AIR_PERMIT_SOURCES:
            permit = (
                f"Air permit {link.source_record_id}"
                if _identifying(link) and link.source_record_id
                else "Air permit"
            )
            stated = _date_text(normalised.get("queue_date")) or _date_text(iso(link.retrieved_at))
            fire("air_permit", link, f"{permit}, {agency}, {stated} (public record).")
        elif source.id in FEDERAL_RECORD_SOURCES:
            named = (
                f"{agency} record {link.source_record_id}"
                if _identifying(link) and link.source_record_id
                else f"{agency} record"
            )
            fire("federal_record", link, f"{named}, status {_status_words(view)}.")
    return out


def live_firings(
    resolved: Sequence[tuple[pm.PrivateProjectLink, Resolution]], entitlement: str
) -> list[Firing]:
    """Every rule that fires on the project's visible linked records, once per (item, record,
    rule). Only proposals carry the facts docs/19 §2.4 reads."""
    seen: set[tuple[str, str, str, str]] = set()
    out: list[Firing] = []
    for link, resolution in resolved:
        if resolution.record_type != "proposal" or not resolution.visible or link.record_public_id is None:
            continue
        for firing in _proposal_firings(resolution.record, link.record_public_id, entitlement):
            if firing.key not in seen:
                seen.add(firing.key)
                out.append(firing)
    return out


def _evidence_states(db: Session, access: authz.ProjectAccess) -> dict[tuple[_uuid.UUID, str, str, str], str]:
    rows = db.execute(
        select(
            pm.PrivateProjectEvidence.item_id,
            pm.PrivateProjectEvidence.record_type,
            pm.PrivateProjectEvidence.record_public_id,
            pm.PrivateProjectEvidence.rule,
            pm.PrivateProjectEvidence.state,
        ).where(pm.PrivateProjectEvidence.project_id == access.project.id)
    ).all()
    return {(item_id, rtype, rid, rule): state for item_id, rtype, rid, rule, state in rows}


def _evidenced_items(
    items: Sequence[pm.PrivateProjectItem],
    firings: Sequence[Firing],
    states: Mapping[tuple[_uuid.UUID, str, str, str], str],
) -> set[_uuid.UUID]:
    by_key = {item.template_key: item for item in items if item.template_key}
    out: set[_uuid.UUID] = set()
    for firing in firings:
        item = by_key.get(firing.item_key)
        if item is None or not checklist.may_suggest(item.template_key, firing.rule):
            continue
        state = states.get((item.id, firing.record_type, firing.stored_public_id, firing.rule), "suggested")
        if state != "dismissed" and item.status != "not_applicable":
            out.add(item.id)
    return out


def _readiness_json(rows: Sequence[checklist.PillarReadiness]) -> list[dict[str, Any]]:
    return [
        {
            "pillar": row.pillar,
            "label": row.label,
            "done": row.done,
            "total": row.total,
            "not_applicable": row.not_applicable,
            "with_suggested_evidence": row.with_suggested_evidence,
            "text": (
                f"{row.done} of {row.total} items done (your status) · "
                f"{row.with_suggested_evidence} with suggested public-record evidence"
            ),
        }
        for row in rows
    ]


# ================================================================== the tracker's live columns
#: Event types that state a change of status: the lifecycle, a listing, a plant's retirement.
STATUS_EVENT_TYPES: Final = frozenset(
    {
        "status_change",
        "delisted",
        "retirement_planned",
        "retirement_date_changed",
        "retirement_cancelled",
        "retired",
        "returned_to_service",
    }
)
STATUS_KEYS: Final = frozenset({"lifecycle_state", "status"})
DEADLINE_KEYS: Final = frozenset({"due_at"})
#: What a change says, by reason; a row with several shows the first of `CHANGE_PRIORITY`.
CHANGE_TEXT: Final[Mapping[str, str]] = {
    "deadline": "Deadline moved",
    "status": "Status changed",
    "updated": "Record updated",
}
CHANGE_PRIORITY: Final = ("deadline", "status", "updated")
#: The public records whose change history the tracker reads (each has `event` rows of its own).
EVENT_RECORD_TYPES: Final = ("proposal", "opportunity", "asset")


def _instant(value: object) -> dt.datetime | None:
    """A served `due_at` as a UTC instant. The gated serialiser serves the stored column, or, when
    it re-derives the value from the readable sources' rows, the date or timestamp text they state."""
    if isinstance(value, dt.datetime):
        return pm.as_utc(value)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=dt.UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return pm.as_utc(parsed)
    return None


def change_reason(event: Event) -> str:
    """`deadline` when the record's due date changed, `status` when its stated status did (or a
    listing or retirement event), else `updated`."""
    keys = set(event.changed_keys or [])
    if keys & DEADLINE_KEYS:
        return "deadline"
    if event.event_type in STATUS_EVENT_TYPES or keys & STATUS_KEYS:
        return "status"
    return "updated"


@dataclass(frozen=True)
class LiveRecord:
    """A linked public record as the viewer sees it now (a merged id followed to its survivor).
    `due_at` is the gated serialiser's value (`gated_record`), for an RFP or funding notice."""

    record_type: str
    public_id: str
    row_id: _uuid.UUID
    name: str | None
    url: str
    due_at: dt.datetime | None = None


@dataclass(frozen=True)
class Change:
    """One public change event on a linked record, visible to the viewer, since their last visit."""

    record: LiveRecord
    reason: str
    at: dt.datetime
    event: Event = field(repr=False, compare=False)


@dataclass(frozen=True)
class Deadline:
    """An item's next deadline: the soonest upcoming `due_at` of a linked notice, its own or, for a
    project, one of its open bids' (`via`)."""

    due_at: dt.datetime
    record: LiveRecord
    via: authz.ProjectAccess | None = field(default=None, compare=False)


class TrackerColumns:
    """The two live columns of the tracker, next deadline and "changed since your last visit", for
    many items at once: the items' links in one query, their linked proposals, notices and plants in
    one query per type (merged ids followed one by one), and the visible change events in one query
    per history. Every record is read through its own visibility predicate at the viewer's tier, and
    every event through the predicate the public feeds use, so nothing here is more than the public
    pages show the same viewer.

    A deadline counts when it is today or later and the item is not closed (`kinds.CLOSED_STAGES`).
    A project also takes the soonest deadline of the open, unarchived bids attached to it that are
    among `accesses` (bids the viewer is a member of). A change counts when the event became visible to
    the viewer's tier after the member last opened the item, or joined it before any visit."""

    def __init__(
        self,
        db: Session,
        entitlement: str,
        accesses: Sequence[authz.ProjectAccess],
        *,
        now: dt.datetime | None = None,
    ) -> None:
        self._db = db
        self._entitlement = entitlement
        self._now = now or pm.utcnow()
        self._accesses = list(accesses)
        project_ids = authz.granted_project_ids(self._accesses)
        self._links: dict[_uuid.UUID, list[pm.PrivateProjectLink]] = {pid: [] for pid in project_ids}
        if project_ids:
            rows = db.execute(
                select(pm.PrivateProjectLink)
                .where(pm.PrivateProjectLink.project_id.in_(project_ids))
                .order_by(pm.PrivateProjectLink.created_at, pm.PrivateProjectLink.id)
            ).scalars()
            for link in rows:
                self._links[link.project_id].append(link)
        self._records = self._read_records()
        self._events = self._read_events()
        self._own: dict[_uuid.UUID, Deadline | None] = {}

    # -- reading
    def _refs(self, record_type: str) -> list[str]:
        return sorted(
            {
                link.record_public_id
                for links in self._links.values()
                for link in links
                if link.record_type == record_type and link.record_public_id
            }
        )

    def _read_records(self) -> dict[tuple[str, str], LiveRecord | None]:
        db, entitlement = self._db, self._entitlement
        out: dict[tuple[str, str], LiveRecord | None] = {}
        if wanted := self._refs("proposal"):
            props = db.scalars(
                select(Proposal)
                .where(Proposal.public_id.in_(wanted), *proposal_visibility_filter(entitlement))
                .options(selectinload(Proposal.sources))
            ).all()
            found = {row.public_id: row for row in props}
            for public_id in wanted:
                prop = found.get(public_id) or merged_survivor(
                    db, Proposal, public_id, proposal_visibility_filter, entitlement
                )
                out[("proposal", public_id)] = self._proposal(prop) if isinstance(prop, Proposal) else None
        if wanted := self._refs("opportunity"):
            opps = db.scalars(
                select(Opportunity)
                .where(Opportunity.public_id.in_(wanted), *opportunity_visibility_filter(entitlement))
                .options(selectinload(Opportunity.sources))
            ).all()
            found_opps = {row.public_id: row for row in opps}
            for public_id in wanted:
                opp = found_opps.get(public_id) or merged_survivor(
                    db, Opportunity, public_id, opportunity_visibility_filter, entitlement
                )
                out[("opportunity", public_id)] = (
                    self._opportunity(opp) if isinstance(opp, Opportunity) else None
                )
        if wanted := self._refs("asset"):
            # Asset pages read the public tier for every caller; so does this.
            assets = {
                row.public_id: row
                for row in db.scalars(
                    select(Asset).where(Asset.public_id.in_(wanted), *asset_visibility_filter())
                ).all()
            }
            for public_id in wanted:
                asset = assets.get(public_id)
                out[("asset", public_id)] = (
                    LiveRecord(
                        "asset", asset.public_id, asset.id, asset.name, f"{WEB_HOST}/assets/{asset.slug}"
                    )
                    if asset is not None
                    else None
                )
        return out

    def _proposal(self, prop: Proposal) -> LiveRecord:
        view = gated_record(prop, self._entitlement)
        return LiveRecord(
            "proposal", prop.public_id, prop.id, view.name_canonical, f"{WEB_HOST}/proposals/{prop.slug}"
        )

    def _opportunity(self, opp: Opportunity) -> LiveRecord:
        view = gated_record(opp, self._entitlement)
        return LiveRecord(
            "opportunity",
            opp.public_id,
            opp.id,
            view.title,
            f"{WEB_HOST}/opportunities/{opp.slug}",
            _instant(view.due_at),
        )

    def _read_events(self) -> dict[_uuid.UUID, list[tuple[Event, dt.datetime]]]:
        """Visible change events on the linked records, newer than the oldest reference time."""
        references = [self.reference(access) for access in self._accesses]
        out: dict[_uuid.UUID, list[tuple[Event, dt.datetime]]] = {}
        if not references:
            return out
        since = min(references)
        public = self._entitlement == "public"
        shown = Event.public_at if public else Event.published_at
        records = [record for record in self._records.values() if record is not None]
        for history, record_types in (("record", ("proposal", "opportunity")), ("asset", ("asset",))):
            row_ids = sorted({r.row_id for r in records if r.record_type in record_types}, key=str)
            if not row_ids:
                continue
            visible = (
                event_visibility_filter(self._entitlement, self._now)
                if history == "record"
                else asset_event_visibility_filter("public", self._now)
            )
            shown_column = shown if history == "record" else Event.public_at
            rows = self._db.execute(
                select(Event, shown_column)
                .where(Event.subject_id.in_(row_ids), *visible, shown_column > since)
                .order_by(shown_column.desc(), Event.seq.desc())
            ).all()
            for event, at in rows:
                if at is not None:
                    out.setdefault(event.subject_id, []).append((event, pm.as_utc(at)))
        return out

    # -- per item
    def reference(self, access: authz.ProjectAccess) -> dt.datetime:
        """What "since your last visit" counts from: the member's last visit, or when they joined."""
        member = access.member
        return pm.as_utc(member.last_viewed_at or member.added_at)

    def live(self, link: pm.PrivateProjectLink) -> LiveRecord | None:
        if link.record_public_id is None:
            return None
        return self._records.get((link.record_type, link.record_public_id))

    def _linked(self, access: authz.ProjectAccess) -> list[LiveRecord]:
        seen: dict[tuple[str, str], LiveRecord] = {}
        for link in self._links.get(access.project.id, []):
            record = self.live(link)
            if record is not None:
                seen.setdefault((record.record_type, record.public_id), record)
        return list(seen.values())

    def own_deadline(self, access: authz.ProjectAccess) -> Deadline | None:
        project = access.project
        if project.id in self._own:
            return self._own[project.id]
        best: Deadline | None = None
        if not item_kinds.is_closed(project.kind, project.stage):
            for record in self._linked(access):
                due = record.due_at
                if due is None or due.date() < self._now.date():
                    continue
                if best is None or due < best.due_at:
                    best = Deadline(due, record)
        self._own[project.id] = best
        return best

    def next_deadline(self, access: authz.ProjectAccess) -> Deadline | None:
        best = self.own_deadline(access)
        project = access.project
        if project.kind != "project":
            return best
        for other in self._accesses:
            bid = other.project
            # An archived bid is off the tracker, so its deadline is not the project's: the list
            # and the project's own page agree whichever bids they were handed.
            if bid.kind != "bid" or bid.parent_project_id != project.id or bid.archived_at is not None:
                continue
            through = self.own_deadline(other)
            if through is not None and (best is None or through.due_at < best.due_at):
                best = Deadline(through.due_at, through.record, via=other)
        return best

    def changes(self, access: authz.ProjectAccess) -> list[Change]:
        """Newest first, at most one entry per event."""
        since = self.reference(access)
        out: list[Change] = []
        for record in self._linked(access):
            for event, at in self._events.get(record.row_id, []):
                if at > since:
                    out.append(Change(record, change_reason(event), at, event))
        out.sort(key=lambda change: (change.at, change.event.seq), reverse=True)
        return out


def _deadline_json(deadline: Deadline | None, now: dt.datetime) -> dict[str, Any] | None:
    if deadline is None:
        return None
    via = deadline.via
    return {
        "due_at": iso(deadline.due_at),
        "days_left": (deadline.due_at.date() - now.date()).days,
        "record": {
            "record_type": deadline.record.record_type,
            "public_id": deadline.record.public_id,
            "name": deadline.record.name,
            "url": deadline.record.url,
        },
        "via_bid": (
            {
                "public_id": via.project.public_id,
                "title": via.project.title,
                "url": _project_url(via.project.public_id),
            }
            if via is not None
            else None
        ),
    }


def _changed_json(changes: Sequence[Change]) -> dict[str, Any]:
    """The list's badge: whether anything changed, the reason that asks most of the user, and how
    many changes of that reason."""
    if not changes:
        return {"changed": False, "reason": None, "text": None, "count": 0, "at": None}
    reasons = {change.reason for change in changes}
    reason = next(r for r in CHANGE_PRIORITY if r in reasons)
    count = sum(1 for change in changes if change.reason == reason)
    latest = max(change.at for change in changes if change.reason == reason)
    return {"changed": True, "reason": reason, "text": CHANGE_TEXT[reason], "count": count, "at": iso(latest)}


def _change_json(change: Change) -> dict[str, Any]:
    event = change.event
    source = (
        _quartet(
            provenance_quartet(
                event.source,
                event.licence,
                source_url=event.source_url or event.source.url,
                retrieved_at=event.retrieved_at or event.observed_at,
            )
        )
        if event.source is not None and event.licence is not None
        else None
    )
    return {
        "record": {
            "record_type": change.record.record_type,
            "public_id": change.record.public_id,
            "name": change.record.name,
            "url": change.record.url,
        },
        "reason": change.reason,
        "text": CHANGE_TEXT[change.reason],
        "event_type": event.event_type,
        "changed_keys": list(event.changed_keys or []),
        "at": iso(change.at),
        "source": source,
    }


# ============================================================================== serialisers
def _project_core(access: authz.ProjectAccess) -> dict[str, Any]:
    project = access.project
    return {
        "public_id": project.public_id,
        "url": _project_url(project.public_id),
        "title": project.title,
        "kind": project.kind,
        "kind_label": item_kinds.KIND_LABELS[project.kind],
        "technology": project.technology,
        "capacity_mw": _float(project.capacity_mw),
        "storage_mwh": _float(project.storage_mwh),
        "stage": project.stage,
        "stage_label": item_kinds.STAGE_LABELS[project.stage],
        "closed": item_kinds.is_closed(project.kind, project.stage),
        "submitted_on": project.submitted_on.isoformat() if project.submitted_on else None,
        "role": access.role,
        "archived": project.archived_at is not None,
        "updated_at": _when(project.updated_at),
    }


def _stages_json(kind: str) -> list[dict[str, Any]]:
    return [
        {"stage": stage, "label": item_kinds.STAGE_LABELS[stage], "closed": item_kinds.is_closed(kind, stage)}
        for stage in item_kinds.KIND_STAGES[kind]
    ]


def _item_ref(access: authz.ProjectAccess) -> dict[str, Any]:
    project = access.project
    return {
        "public_id": project.public_id,
        "url": _project_url(project.public_id),
        "title": project.title,
        "kind": project.kind,
        "stage": project.stage,
        "stage_label": item_kinds.STAGE_LABELS[project.stage],
        "archived": project.archived_at is not None,
    }


def _project_detail(
    db: Session, access: authz.ProjectAccess, ctx: AuthContext, *, now: dt.datetime | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The item read live. Its next deadline and its changes since the viewer's last visit are read
    as the tracker reads them (`TrackerColumns`); a project's bids are those the viewer is a member
    of, and a bid's project is named only to a viewer who is a member of it too."""
    project = access.project
    now = now or pm.utcnow()
    parent = authz.readable_parent(db, access) if project.kind == "bid" else None
    bids = authz.readable_bids(db, access) if project.kind == "project" else []
    columns = TrackerColumns(db, ctx.entitlement, [access, *bids], now=now)
    changes = columns.changes(access)
    resolved = _resolve_all(db, _project_links(db, access), ctx.entitlement)
    items = checklist.project_items(db, access)
    evidenced = _evidenced_items(items, live_firings(resolved, ctx.entitlement), _evidence_states(db, access))
    readiness = checklist.readiness(db, access, evidence_item_ids=evidenced)
    members = authz.member_counts(db, [access]).get(project.id, 1)
    completeness = next(row for row in readiness if row.pillar == "completeness")
    sight = _Sight(db, ctx.entitlement, (resolution for _link, resolution in resolved))
    data = {
        **_project_core(access),
        "lat": _float(project.lat),
        "lon": _float(project.lon),
        "point_precision": project.point_precision,
        "origin": (
            {
                "record_type": project.origin_type,
                "public_id": sight.shown_id(project.origin_type, project.origin_public_id),
            }
            if project.origin_type
            else None
        ),
        "created_at": _when(project.created_at),
        "archived_at": _when(project.archived_at),
        "member_count": members,
        "shared": members > 1,
        "links": [_link_json(link, resolution) for link, resolution in resolved],
        "readiness": _readiness_json(readiness),
        "progress": _progress_json(completeness.done, completeness.total),
        "status_disclaimer": checklist.STATUS_DISCLAIMER,
        "stages": _stages_json(project.kind),
        "parent": _item_ref(parent) if parent is not None else None,
        "bids": [
            {**_item_ref(bid), "next_deadline": _deadline_json(columns.own_deadline(bid), now)}
            for bid in bids
        ],
        "next_deadline": _deadline_json(columns.next_deadline(access), now),
        "changed_since_last_visit": _changed_json(changes),
        "changes": [_change_json(change) for change in changes],
        "last_viewed_at": _when(access.member.last_viewed_at),
        "email_changes": access.member.email_changes,
    }
    rows = [row for _link, resolution in resolved for row in resolution.licence_rows]
    return data, rows


def _member_ids(db: Session, project_id: _uuid.UUID) -> set[_uuid.UUID]:
    return set(
        db.execute(
            select(pm.PrivateProjectMember.user_id).where(pm.PrivateProjectMember.project_id == project_id)
        ).scalars()
    )


def _people(db: Session, user_ids: Iterable[_uuid.UUID | None]) -> dict[_uuid.UUID, User]:
    wanted = sorted({uid for uid in user_ids if uid is not None}, key=str)
    if not wanted:
        return {}
    return {user.id: user for user in db.execute(select(User).where(User.id.in_(wanted))).scalars()}


def _person(
    user_id: _uuid.UUID | None, people: Mapping[_uuid.UUID, User], members: set[_uuid.UUID]
) -> dict[str, Any]:
    """An author or actor as the members see them: a current member by name; anyone else, an erased
    account included, as "Former member" with nothing that identifies them (owner, 2026-10-22)."""
    user = people.get(user_id) if user_id is not None else None
    if user is None or user_id not in members:
        return {"user_public_id": None, "name": FORMER_MEMBER, "former_member": True}
    return {"user_public_id": user.public_id, "name": user.name, "former_member": False}


def _note_json(
    note: pm.PrivateProjectNote,
    viewer: User,
    people: Mapping[_uuid.UUID, User],
    members: set[_uuid.UUID],
) -> dict[str, Any]:
    return {
        "note_id": note.public_id,
        "body": note.body,
        "author": _person(note.author_user_id, people, members),
        "mine": note.author_user_id == viewer.id,
        "created_at": _when(note.created_at),
        "updated_at": _when(note.updated_at),
        "edited": pm.as_utc(note.updated_at) > pm.as_utc(note.created_at),
    }


def _member_json(view: authz.MemberView, viewer: User) -> dict[str, Any]:
    return {
        "user_public_id": view.user_public_id,
        "name": view.name,
        "role": view.role,
        "added_at": _when(view.added_at),
        "is_you": view.user_public_id == viewer.public_id,
    }


def _invitation_json(row: pm.PrivateProjectInvitation) -> dict[str, Any]:
    """Never `token_hash` or `email_hash` (P1 interface)."""
    return {
        "invitation_id": row.public_id,
        "email": row.email,
        "role": row.role,
        "created_at": _when(row.created_at),
        "expires_at": _when(row.expires_at),
    }


def _item_json(
    item: pm.PrivateProjectItem, guidance: Mapping[str, str], suggestions: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "item_id": str(item.id),
        "template_key": item.template_key,
        "name": item.name,
        "folder": item.folder,
        "pillar": item.pillar,
        "status": item.status,
        "status_label": checklist.SHARED_STATUS_LABELS[item.status],
        "custom": item.template_key is None,
        "guidance": guidance.get(item.template_key or ""),
        "updated_at": _when(item.updated_at),
        "suggestions": suggestions,
    }


def _suggestion_json(firing: Firing, row: pm.PrivateProjectEvidence | None) -> dict[str, Any]:
    return {
        "evidence_id": str(row.id) if row is not None else None,
        "state": row.state if row is not None else "suggested",
        "rule": firing.rule,
        "label": checklist.EVIDENCE_LABEL,
        "text": firing.text,
        "record": firing.record,
        "source": firing.source,
        "retrieved_at": firing.source.get("retrieved_at"),
    }


# ================================================================================== projects
_ProjectRead = Annotated[authz.ProjectAccess, Depends(authz.project_access("read"))]
_ProjectEdit = Annotated[authz.ProjectAccess, Depends(authz.project_access("edit"))]
_ProjectInvite = Annotated[authz.ProjectAccess, Depends(authz.project_access("invite"))]
_ProjectDelete = Annotated[authz.ProjectAccess, Depends(authz.project_access("delete"))]
_Db = Annotated[Session, Depends(get_db)]
_Ctx = Annotated[AuthContext, Depends(get_auth_context)]
_User = Annotated[User, Depends(signed_in_user)]
_Body = Annotated[dict[str, Any], Body()]


def _kinds_param(request: Request) -> list[str] | None:
    raw = request.query_params.get("kind")
    if raw is None:
        return None
    wanted = [part.strip() for part in raw.split(",")]
    if not all(part in item_kinds.ITEM_KINDS for part in wanted):
        raise _invalid("kind", "kind is one or more of " + ", ".join(item_kinds.ITEM_KINDS))
    return wanted


def _tracker_order(row: Mapping[str, Any]) -> tuple[int, str, float]:
    """Next deadline soonest first; then, and for items without one, the most recent change first."""
    deadline = row["next_deadline"]
    updated = dt.datetime.fromisoformat(str(row["updated_at"]).replace("Z", "+00:00")).timestamp()
    if deadline is None:
        return (1, "", -updated)
    return (0, str(deadline["due_at"]), -updated)


@router.get("/v1/my/projects")
def list_my_projects(request: Request, db: _Db, ctx: _Ctx, user: _User) -> Any:
    """The tracker: every item the caller owns or was invited to, with its kind, stage, next
    deadline and what changed on its linked records since their last visit; `kind` narrows it."""
    check_allowed(request, {"include_archived", "kind"})
    raw = request.query_params.get("include_archived", "false")
    if raw not in ("true", "false"):
        raise _invalid("include_archived", "include_archived is true or false")
    wanted = _kinds_param(request)
    now = pm.utcnow()
    # Every item is read, whatever the filter: a project's deadline can come from its bids.
    every = authz.member_projects(db, ctx, include_archived=raw == "true")
    accesses = [a for a in every if wanted is None or a.project.kind in wanted]
    columns = TrackerColumns(db, ctx.entitlement, every, now=now)
    counts = authz.member_counts(db, accesses)
    progress = checklist.progress_by_project(db, accesses)
    data = []
    for access in accesses:
        members = counts.get(access.project.id, 1)
        done = progress.get(access.project.id, checklist.Progress(0, 0))
        data.append(
            {
                **_project_core(access),
                "member_count": members,
                "shared": members > 1,
                "progress": _progress_json(done.done, done.total),
                "next_deadline": _deadline_json(columns.next_deadline(access), now),
                "changed_since_last_visit": _changed_json(columns.changes(access)),
            }
        )
    data.sort(key=_tracker_order)
    return _envelope(ctx, data)


@router.get("/v1/my/kinds")
def list_my_kinds(request: Request, user: _User) -> Any:
    """The tracker's vocabulary for the pages: each kind with its stages, and which kinds "Track"
    makes from each kind of public record."""
    check_allowed(request, set())
    data = {
        "kinds": [
            {
                "kind": kind,
                "label": item_kinds.KIND_LABELS[kind],
                "plural": item_kinds.KIND_PLURALS[kind],
                "description": item_kinds.KIND_DESCRIPTIONS[kind],
                "default_stage": item_kinds.DEFAULT_STAGES[kind],
                "stages": _stages_json(kind),
            }
            for kind in item_kinds.ITEM_KINDS
        ],
        "track": {record_type: list(made) for record_type, made in item_kinds.TRACK_KINDS.items()},
        "technologies": list(pm.PROJECT_TECHNOLOGIES),
        "relations": list(pm.LINK_RELATIONS),
    }
    return build_envelope(data, meta=build_meta(tier="public"), licence_summary=build_licence_summary([]))


def _fields_from_body(body: Mapping[str, Any]) -> dict[str, object]:
    return {name: body[name] for name in BODY_FIELDS if name in body}


@router.post("/v1/my/projects", status_code=201)
def create_my_project(db: _Db, ctx: _Ctx, user: _User, body: _Body) -> Any:
    """Blank, or from a public record (`from`): fields the body leaves out are prefilled from that
    record under the licence rule; fields it gives (`null` included) are the user's own. From a
    record, `kind` defaults to what tracking that record makes (`kinds.TRACK_KINDS`)."""
    _only(body, (*BODY_FIELDS, "from"))
    fields = _fields_from_body(body)
    origin = body.get("from")
    prefill: Prefill | None = None
    applied: list[str] = []
    if origin is not None:
        if not isinstance(origin, dict):
            raise _invalid("from", "from is an object: record_type, public_id and relation")
        _only(origin, ("record_type", "public_id", "relation"), prefix="from.")
        prefill = build_prefill(
            db,
            origin.get("record_type"),
            origin.get("public_id"),
            origin.get("relation"),
            ctx.entitlement,
            kind=fields.get("kind"),
        )
        fields["kind"] = prefill.kind
        point_given = "lat" in fields or "lon" in fields
        for name, value in prefill.values.items():
            if name in fields or (point_given and name in ("lat", "lon", "point_precision")):
                continue
            fields[name] = value
            applied.append(name)
    if "title" not in fields:
        raise _invalid("title", "title is required")
    access = authz.create_project(
        db,
        ctx,
        fields=fields,
        origin_type=prefill.record_type if prefill else None,
        origin_public_id=prefill.public_id if prefill else None,
    )
    if prefill is not None:
        _store_link(db, access, prefill.record_type, prefill.public_id, prefill.default_relation)
    data, rows = _project_detail(db, access, ctx)
    data["prefill"] = _prefill_json(prefill, applied=applied) if prefill is not None else None
    return _envelope(ctx, data, rows)


@router.post("/v1/my/prefill")
def preview_prefill(db: _Db, ctx: _Ctx, user: _User, body: _Body) -> Any:
    """What "Track" would prefill from a record, for the form (docs/19 §6.2), without creating
    anything. A POST so the record id travels in the body, not the URL."""
    _only(body, ("record_type", "public_id", "relation", "kind"))
    prefill = build_prefill(
        db,
        body.get("record_type"),
        body.get("public_id"),
        body.get("relation"),
        ctx.entitlement,
        kind=body.get("kind"),
    )
    data = {
        **_prefill_json(prefill),
        "values": dict(prefill.values),
        "record": prefill.resolution.summary,
    }
    return _envelope(ctx, data, prefill.resolution.licence_rows)


@router.get("/v1/my/projects/{project_id}")
def get_my_project(request: Request, db: _Db, ctx: _Ctx, access: _ProjectRead) -> Any:
    """Opening an item: its changes are those since the caller's last visit, and this request then
    becomes that visit (`authz.mark_viewed`)."""
    check_allowed(request, set())
    now = pm.utcnow()
    data, rows = _project_detail(db, access, ctx, now=now)
    authz.mark_viewed(db, access, now=now)
    return _envelope(ctx, data, rows)


@router.patch("/v1/my/projects/{project_id}")
def update_my_project(db: _Db, ctx: _Ctx, access: _ProjectRead, body: _Body) -> Any:
    """Fields (editor), `archived` (owner) and the caller's own `email_changes` (any member).
    Unarchiving runs first and archiving last, so one request can do both with an edit."""
    _only(body, (*BODY_FIELDS, *MEMBER_FIELDS, "archived"))
    archived = body.get("archived")
    if "archived" in body and not isinstance(archived, bool):
        raise _invalid("archived", "archived is true or false")
    fields = _fields_from_body(body)
    if "email_changes" in body:
        authz.set_email_changes(db, access, body["email_changes"])
    if archived is False:
        authz.set_archived(db, access, False)
    if fields:
        authz.update_project(db, access, fields)
    if archived is True:
        authz.set_archived(db, access, True)
    data, rows = _project_detail(db, access, ctx)
    return _envelope(ctx, data, rows)


@router.delete("/v1/my/projects/{project_id}", status_code=204)
def delete_my_project(db: _Db, access: _ProjectDelete) -> Response:
    authz.delete_project(db, access)
    return Response(status_code=204)


# ===================================================================================== links
def _store_link(
    db: Session, access: authz.ProjectAccess, record_type: str, public_id: str, relation: str
) -> tuple[pm.PrivateProjectLink, bool]:
    """The link, and whether it is new. An identical link is returned as it stands (a retry adds
    nothing, and so does a concurrent add of the same link); the 51st is refused.

    Concurrent adds of one link used to `500` on the unique link constraint (lane P2 security
    review, 2026-10-23). On Postgres, `lock_project` serialises them, and the second request looks
    again once it holds the lock. SQLite takes no row lock, so the insert also runs in a savepoint,
    and a conflict there returns the link the other request stored."""
    authz.require_action(access, "edit")

    def same_link() -> pm.PrivateProjectLink | None:
        return db.execute(
            select(pm.PrivateProjectLink).where(
                pm.PrivateProjectLink.project_id == access.project.id,
                pm.PrivateProjectLink.record_type == record_type,
                pm.PrivateProjectLink.record_public_id == public_id,
                pm.PrivateProjectLink.relation == relation,
            )
        ).scalar_one_or_none()

    existing = same_link()
    if existing is not None:
        return existing, False
    authz.lock_project(db, access.project)
    existing = same_link()  # committed while this request waited for the lock
    if existing is not None:
        return existing, False
    count = db.scalar(
        select(func.count())
        .select_from(pm.PrivateProjectLink)
        .where(pm.PrivateProjectLink.project_id == access.project.id)
    )
    if (count or 0) >= pm.MAX_LINKS_PER_PROJECT:
        raise ProblemError(
            "conflict",
            "Link limit reached",
            detail=f"At most {pm.MAX_LINKS_PER_PROJECT} linked records per project; remove one first.",
        )
    link = pm.PrivateProjectLink(
        project_id=access.project.id,
        record_type=record_type,
        record_public_id=public_id,
        relation=relation,
        added_by_user_id=access.user.id,
    )
    db.flush()  # anything already pending stays outside the savepoint and its handler
    try:
        with db.begin_nested():
            db.add(link)
            db.flush()
    except IntegrityError:
        existing = same_link()
        if existing is None:
            raise
        return existing, False
    pm.add_activity(
        db,
        project_id=access.project.id,
        actor_user_id=access.user.id,
        action="link_added",
        detail={
            "link_id": str(link.id),
            "record_type": record_type,
            "record_public_id": public_id,
            "relation": relation,
        },
    )
    access.project.updated_at = pm.utcnow()
    db.flush()
    return link, True


@router.get("/v1/my/projects/{project_id}/links")
def list_my_project_links(request: Request, db: _Db, ctx: _Ctx, access: _ProjectRead) -> Any:
    check_allowed(request, set())
    resolved = _resolve_all(db, _project_links(db, access), ctx.entitlement)
    rows = [row for _link, resolution in resolved for row in resolution.licence_rows]
    return _envelope(ctx, [_link_json(link, resolution) for link, resolution in resolved], rows)


@router.post("/v1/my/projects/{project_id}/links", status_code=201)
def add_my_project_link(db: _Db, ctx: _Ctx, access: _ProjectEdit, body: _Body, response: Response) -> Any:
    """A reference to a public record the caller can see now. A merged id is stored as its
    survivor's. Corridors arrive with lane P6 (docs/19 §4.2)."""
    _only(body, ("record_type", "public_id", "relation"))
    record_type = _text(body, "record_type")
    if record_type == "corridor":
        raise _invalid("record_type", "corridor links arrive with the site finder (docs/19 §4.2)")
    if record_type not in pm.LINK_RECORD_TYPES:
        raise _invalid(
            "record_type",
            "record_type is one of " + ", ".join(t for t in pm.LINK_RECORD_TYPES if t != "corridor"),
        )
    public_id = _text(body, "public_id")
    if not pm.is_record_public_id(record_type, public_id):
        raise _invalid("public_id", "public_id is not an id of that record type")
    relation = _text(body, "relation")
    if relation not in pm.LINK_RELATIONS:
        raise _invalid("relation", "relation is one of " + ", ".join(pm.LINK_RELATIONS))
    resolution = resolve_reference(db, record_type, public_id, ctx.entitlement)
    if not resolution.visible:
        raise _invalid("public_id", "no such record is visible")
    stored_id = resolution.survivor_public_id or public_id
    link, created = _store_link(db, access, record_type, stored_id, relation)
    if not created:
        response.status_code = 200
    live = resolve_link(db, link, ctx.entitlement)
    return _envelope(ctx, _link_json(link, live), live.licence_rows)


@router.delete("/v1/my/projects/{project_id}/links/{link_id}", status_code=204)
def remove_my_project_link(link_id: str, db: _Db, access: _ProjectEdit) -> Response:
    authz.require_action(access, "edit")
    link = db.execute(
        select(pm.PrivateProjectLink).where(
            pm.PrivateProjectLink.id == _uuid_or_404(link_id),
            pm.PrivateProjectLink.project_id == access.project.id,
        )
    ).scalar_one_or_none()
    if link is None:
        raise authz.project_not_found()
    record_type, public_id, removed_id = link.record_type, link.record_public_id, str(link.id)
    db.delete(link)
    db.flush()
    remaining = db.scalar(
        select(func.count())
        .select_from(pm.PrivateProjectLink)
        .where(
            pm.PrivateProjectLink.project_id == access.project.id,
            pm.PrivateProjectLink.record_type == record_type,
            pm.PrivateProjectLink.record_public_id == public_id,
        )
    )
    if not remaining and public_id is not None:
        # Evidence rests on a link; with the record no longer linked its references go too.
        db.execute(
            delete(pm.PrivateProjectEvidence).where(
                pm.PrivateProjectEvidence.project_id == access.project.id,
                pm.PrivateProjectEvidence.record_type == record_type,
                pm.PrivateProjectEvidence.record_public_id == public_id,
            )
        )
    detail: dict[str, object] = {"link_id": removed_id, "record_type": record_type}
    if public_id is not None:
        detail["record_public_id"] = public_id
    pm.add_activity(
        db, project_id=access.project.id, actor_user_id=access.user.id, action="link_removed", detail=detail
    )
    access.project.updated_at = pm.utcnow()
    db.flush()
    return Response(status_code=204)


# ================================================================================= checklist
_EvidenceKey = tuple[_uuid.UUID, str, str, str]


def _evidence_key(item: pm.PrivateProjectItem, firing: Firing) -> _EvidenceKey:
    return (item.id, firing.record_type, firing.stored_public_id, firing.rule)


def _stored_evidence(
    db: Session, access: authz.ProjectAccess
) -> dict[_EvidenceKey, pm.PrivateProjectEvidence]:
    rows = db.execute(
        select(pm.PrivateProjectEvidence).where(pm.PrivateProjectEvidence.project_id == access.project.id)
    ).scalars()
    return {(row.item_id, row.record_type, row.record_public_id, row.rule): row for row in rows}


def _hold_for_new_evidence(db: Session, access: authz.ProjectAccess) -> None:
    """Serialises the views that store a project's new suggestions, so a second view finds the
    first view's rows instead of inserting them again. Postgres: `lock_project`. SQLite has no row
    locks, so it takes the database's write lock instead, with a write that changes nothing.

    On SQLite that write must open the transaction. pysqlite runs the reads before it outside
    any transaction, so the write waits for the lock. A transaction that has already read (as one
    a savepoint opened first would have) and then asks for the lock while another connection holds
    it is refused at once ("database is locked"): 7 of 80 parallel first views on a live server
    before this hold."""
    authz.lock_project(db, access.project)
    if db.get_bind().dialect.name == "sqlite":
        db.execute(
            update(pm.PrivateProject)
            .where(pm.PrivateProject.id == access.project.id)
            .values(updated_at=pm.PrivateProject.updated_at)
            .execution_options(synchronize_session=False)
        )


def _record_suggestion(
    db: Session, access: authz.ProjectAccess, item: pm.PrivateProjectItem, firing: Firing
) -> pm.PrivateProjectEvidence | None:
    """`checklist.record_suggestion` for a suggestion with no stored row yet, called under
    `_hold_for_new_evidence`.

    Before 2026-10-23, the first views after a new link all found no row and all inserted one.
    Every insert after the first broke the unique evidence constraint and gave a `500`; on
    Postgres the later inserts waited on the index for the first to commit, then failed (lane P2
    security review). The hold makes later views find the row. The insert also runs in a
    savepoint, so if a writer that does not take the hold stores the row first, the request's
    transaction stays usable and the second call reads that row back."""
    kwargs: dict[str, Any] = {
        "item_id": item.id,
        "record_type": firing.record_type,
        "record_public_id": firing.stored_public_id,
        "rule": firing.rule,
    }
    db.flush()  # anything already pending stays outside the savepoint and its handler
    try:
        with db.begin_nested():
            return checklist.record_suggestion(db, access, **kwargs)
    except IntegrityError:
        return checklist.record_suggestion(db, access, **kwargs)


def _checklist_json(
    db: Session, access: authz.ProjectAccess, ctx: AuthContext
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    resolved = _resolve_all(db, _project_links(db, access), ctx.entitlement)
    firings = live_firings(resolved, ctx.entitlement)
    items = checklist.project_items(db, access)
    by_key = {item.template_key: item for item in items if item.template_key}
    shown: dict[_uuid.UUID, list[dict[str, Any]]] = {item.id: [] for item in items}
    evidenced: set[_uuid.UUID] = set()
    pairs = [
        (item, firing)
        for firing in firings
        if (item := by_key.get(firing.item_key)) is not None
        and checklist.may_suggest(item.template_key, firing.rule)
    ]
    stored: dict[_EvidenceKey, pm.PrivateProjectEvidence | None] = dict(_stored_evidence(db, access))
    missing = [(item, firing) for item, firing in pairs if _evidence_key(item, firing) not in stored]
    if missing and access.project.archived_at is None:
        _hold_for_new_evidence(db, access)
    for item, firing in missing:
        stored[_evidence_key(item, firing)] = _record_suggestion(db, access, item, firing)
    for item, firing in pairs:
        row = stored.get(_evidence_key(item, firing))
        if row is not None and row.state == "dismissed":
            continue
        shown[item.id].append(_suggestion_json(firing, row))
        if item.status != "not_applicable":
            evidenced.add(item.id)
    guidance = checklist.guidance_for(db, [item.template_key for item in items if item.template_key])
    folders = []
    for folder in pm.CHECKLIST_FOLDERS:
        mine = [item for item in items if item.folder == folder]
        applicable = [item for item in mine if item.status != "not_applicable"]
        folders.append(
            {
                "folder": folder,
                "label": checklist.FOLDER_LABELS[folder],
                "done": sum(1 for item in applicable if item.status == "done"),
                "total": len(applicable),
                "items": [_item_json(item, guidance, shown[item.id]) for item in mine],
            }
        )
    readiness = checklist.readiness(db, access, evidence_item_ids=evidenced)
    data = {
        "project_id": access.project.public_id,
        "status_disclaimer": checklist.STATUS_DISCLAIMER,
        "evidence_label": checklist.EVIDENCE_LABEL,
        "status_labels": dict(checklist.SHARED_STATUS_LABELS),
        "folders": folders,
        "readiness": _readiness_json(readiness),
    }
    rows = [row for _link, resolution in resolved for row in resolution.licence_rows]
    return data, rows


@router.get("/v1/my/projects/{project_id}/checklist")
def get_my_checklist(request: Request, db: _Db, ctx: _Ctx, access: _ProjectRead) -> Any:
    check_allowed(request, set())
    data, rows = _checklist_json(db, access, ctx)
    return _envelope(ctx, data, rows)


@router.post("/v1/my/projects/{project_id}/checklist", status_code=201)
def add_my_checklist_item(db: _Db, ctx: _Ctx, access: _ProjectEdit, body: _Body) -> Any:
    _only(body, ("name", "folder", "pillar"))
    name = _text(body, "name")
    folder = _text(body, "folder")
    pillar = body.get("pillar")
    if pillar is not None and not isinstance(pillar, str):
        raise _invalid("pillar", "pillar is one of " + ", ".join(pm.ITEM_PILLARS))
    item = checklist.add_custom_item(db, access, name=name, folder=folder, pillar=pillar)
    access.project.updated_at = pm.utcnow()
    return _envelope(ctx, _item_json(item, {}, []))


@router.patch("/v1/my/projects/{project_id}/checklist/{item_id}")
def set_my_checklist_status(item_id: str, db: _Db, ctx: _Ctx, access: _ProjectEdit, body: _Body) -> Any:
    _only(body, ("status",))
    status = _text(body, "status")
    item = checklist.set_item_status(db, access, _uuid_or_404(item_id), status)
    access.project.updated_at = pm.utcnow()
    return _envelope(ctx, _item_json(item, {}, []))


def _decide(
    item_id: str, evidence_id: str, decision: str, db: Session, ctx: AuthContext, access: authz.ProjectAccess
) -> Any:
    row = checklist.decide_evidence(
        db, access, item_id=_uuid_or_404(item_id), evidence_id=_uuid_or_404(evidence_id), decision=decision
    )
    data = {
        "evidence_id": str(row.id),
        "item_id": str(row.item_id),
        "state": row.state,
        "rule": row.rule,
        "label": checklist.EVIDENCE_LABEL,
        "record": {"record_type": row.record_type, "public_id": row.record_public_id},
        "decided_at": _when(row.decided_at),
    }
    return _envelope(ctx, data)


@router.post("/v1/my/projects/{project_id}/checklist/{item_id}/evidence/{evidence_id}/accept")
def accept_my_evidence(item_id: str, evidence_id: str, db: _Db, ctx: _Ctx, access: _ProjectEdit) -> Any:
    """Keeps the suggestion on the item; the item's status is unchanged (docs/19 §2.4)."""
    return _decide(item_id, evidence_id, "accepted", db, ctx, access)


@router.post("/v1/my/projects/{project_id}/checklist/{item_id}/evidence/{evidence_id}/dismiss")
def dismiss_my_evidence(item_id: str, evidence_id: str, db: _Db, ctx: _Ctx, access: _ProjectEdit) -> Any:
    """Hides the suggestion; the item's status is unchanged (docs/19 §2.4)."""
    return _decide(item_id, evidence_id, "dismissed", db, ctx, access)


# ===================================================================================== notes
def _note_body(body: Mapping[str, Any]) -> str:
    _only(body, ("body",))
    text = body.get("body")
    if not isinstance(text, str) or not text.strip():
        raise _invalid("body", "body is required")
    if len(text) > pm.NOTE_MAX_CHARS:
        raise _invalid("body", f"body is at most {pm.NOTE_MAX_CHARS:,} characters")
    if pm.has_control_characters(text, allow_line_breaks=True):
        raise _invalid("body", "body contains a control character")
    return text


def _note_or_404(db: Session, access: authz.ProjectAccess, note_id: str) -> pm.PrivateProjectNote:
    if not pm.is_private_public_id(pm.NOTE_ID_PREFIX, note_id):
        raise authz.project_not_found()
    note = db.execute(
        select(pm.PrivateProjectNote).where(
            pm.PrivateProjectNote.public_id == note_id, pm.PrivateProjectNote.project_id == access.project.id
        )
    ).scalar_one_or_none()
    if note is None:
        raise authz.project_not_found()
    return note


def _note_response(
    db: Session, ctx: AuthContext, access: authz.ProjectAccess, note: pm.PrivateProjectNote
) -> Any:
    members = _member_ids(db, access.project.id)
    return _envelope(ctx, _note_json(note, access.user, _people(db, [note.author_user_id]), members))


@router.get("/v1/my/projects/{project_id}/notes")
def list_my_notes(request: Request, db: _Db, ctx: _Ctx, access: _ProjectRead) -> Any:
    check_allowed(request, set())
    notes = list(
        db.execute(
            select(pm.PrivateProjectNote)
            .where(pm.PrivateProjectNote.project_id == access.project.id)
            .order_by(pm.PrivateProjectNote.created_at.desc(), pm.PrivateProjectNote.id.desc())
        ).scalars()
    )
    members = _member_ids(db, access.project.id)
    people = _people(db, [note.author_user_id for note in notes])
    return _envelope(ctx, [_note_json(note, access.user, people, members) for note in notes])


@router.post("/v1/my/projects/{project_id}/notes", status_code=201)
def add_my_note(db: _Db, ctx: _Ctx, access: _ProjectEdit, body: _Body) -> Any:
    text = _note_body(body)
    authz.require_action(access, "edit")
    now = pm.utcnow()
    note = pm.PrivateProjectNote(
        project_id=access.project.id, author_user_id=access.user.id, body=text, created_at=now, updated_at=now
    )
    db.add(note)
    db.flush()
    pm.add_activity(
        db,
        project_id=access.project.id,
        actor_user_id=access.user.id,
        action="note_added",
        detail={"note_id": note.public_id},
        now=now,
    )
    access.project.updated_at = now
    db.flush()
    return _note_response(db, ctx, access, note)


@router.patch("/v1/my/projects/{project_id}/notes/{note_id}")
def edit_my_note(note_id: str, db: _Db, ctx: _Ctx, access: _ProjectEdit, body: _Body) -> Any:
    """Its author only (docs/19 §2.5)."""
    text = _note_body(body)
    note = _note_or_404(db, access, note_id)
    if note.author_user_id != access.user.id:
        raise ProblemError(
            "forbidden_role", "Only its author can edit a note", detail="Only its author can edit a note."
        )
    if note.body != text:
        now = pm.utcnow()
        note.body = text
        note.updated_at = now
        pm.add_activity(
            db,
            project_id=access.project.id,
            actor_user_id=access.user.id,
            action="note_edited",
            detail={"note_id": note.public_id},
            now=now,
        )
        db.flush()
    return _note_response(db, ctx, access, note)


@router.delete("/v1/my/projects/{project_id}/notes/{note_id}", status_code=204)
def delete_my_note(note_id: str, db: _Db, access: _ProjectEdit) -> Response:
    """Its author or the project owner (docs/19 §2.5)."""
    note = _note_or_404(db, access, note_id)
    if note.author_user_id != access.user.id and access.role != "owner":
        raise ProblemError(
            "forbidden_role",
            "Only its author or the owner can delete a note",
            detail="Only its author or the project owner can delete a note.",
        )
    public_id = note.public_id
    db.delete(note)
    pm.add_activity(
        db,
        project_id=access.project.id,
        actor_user_id=access.user.id,
        action="note_deleted",
        detail={"note_id": public_id},
    )
    db.flush()
    return Response(status_code=204)


# =================================================================================== members
def _user_id_or_404(user_id: str) -> str:
    if not re.fullmatch(r"usr_[0-9A-HJKMNP-TV-Z]{10,26}", user_id):
        raise authz.project_not_found()
    return user_id


def _members_response(db: Session, ctx: AuthContext, access: authz.ProjectAccess) -> Any:
    views = authz.project_members(db, access)
    return _envelope(ctx, [_member_json(view, access.user) for view in views])


@router.get("/v1/my/projects/{project_id}/members")
def list_my_members(request: Request, db: _Db, ctx: _Ctx, access: _ProjectRead) -> Any:
    check_allowed(request, set())
    return _members_response(db, ctx, access)


@router.patch("/v1/my/projects/{project_id}/members/{user_id}")
def change_my_member(user_id: str, db: _Db, ctx: _Ctx, access: _ProjectRead, body: _Body) -> Any:
    """Owner: `role` editor or viewer, or `owner` to transfer ownership (the caller becomes an
    editor). Returns the members."""
    _only(body, ("role",))
    role = _text(body, "role")
    target = _user_id_or_404(user_id)
    if role == "owner":
        authz.transfer_ownership(db, access, target)
    else:
        authz.change_member_role(db, access, target, role)
    access.project.updated_at = pm.utcnow()
    db.flush()
    return _members_response(db, ctx, access)


@router.delete("/v1/my/projects/{project_id}/members/{user_id}", status_code=204)
def remove_my_member(user_id: str, db: _Db, access: _ProjectRead) -> Response:
    """Owner removes a member; a member removes themselves (leaving). The owner cannot leave
    (`403 forbidden_role`: transfer first)."""
    authz.remove_member(db, access, _user_id_or_404(user_id))
    return Response(status_code=204)


# =============================================================================== invitations
@router.get("/v1/my/projects/{project_id}/invitations")
def list_my_invitations(request: Request, db: _Db, ctx: _Ctx, access: _ProjectInvite) -> Any:
    check_allowed(request, set())
    return _envelope(ctx, [_invitation_json(row) for row in invitations.list_invitations(db, access)])


@router.post("/v1/my/projects/{project_id}/invitations", status_code=201)
def create_my_invitation(db: _Db, ctx: _Ctx, access: _ProjectInvite, body: _Body) -> Any:
    """Owner. The link is in this response once and nowhere else: never stored, logged or
    replayed (docs/19 §2.6 rules 2 and 7). Limited per user, on top of the daily limit."""
    _only(body, ("email", "role"))
    authz.require_action(access, "invite")
    _limit(f"my-invitations:{access.user.id}", INVITATIONS_PER_HOUR, 3600, "new invitations")
    email, role = body.get("email"), body.get("role")
    if not isinstance(email, str):
        raise _invalid("email", "email is required")
    if not isinstance(role, str):
        raise _invalid("role", "role is editor or viewer")
    issued = invitations.create_invitation(db, access, email=email, role=role)
    data = {**_invitation_json(issued.invitation), "link": issued.link}
    return _envelope(ctx, data)


@router.delete("/v1/my/projects/{project_id}/invitations/{invitation_id}", status_code=204)
def revoke_my_invitation(invitation_id: str, db: _Db, access: _ProjectInvite) -> Response:
    if not pm.is_private_public_id(pm.INVITATION_ID_PREFIX, invitation_id):
        raise authz.project_not_found()
    invitations.revoke_invitation(db, access, invitation_id)
    return Response(status_code=204)


def _token(body: Mapping[str, Any], user: User) -> object:
    _only(body, ("token",))
    _limit(f"my-invitation-attempts:{user.id}", INVITATION_ATTEMPTS_PER_HOUR, 3600, "invitation checks")
    return body.get("token")


@router.post("/v1/my/invitations/preview")
def preview_my_invitation(db: _Db, ctx: _Ctx, user: _User, body: _Body) -> Any:
    """Who invited you, the project's title and the role, once the token and your address both
    check out (docs/19 §6.2). Consumes nothing; the token is in the body, never the path."""
    preview = invitations.preview_invitation(db, ctx, _token(body, user))
    data = {
        "project": {"title": preview.project.title},
        "role": preview.invitation.role,
        "inviter_name": preview.inviter_name,
        "expires_at": _when(preview.invitation.expires_at),
    }
    return _envelope(ctx, data)


@router.post("/v1/my/invitations/accept")
def accept_my_invitation(db: _Db, ctx: _Ctx, user: _User, body: _Body) -> Any:
    access = invitations.accept_invitation(db, ctx, _token(body, user))
    data = {
        "project": {
            "public_id": access.project.public_id,
            "url": _project_url(access.project.public_id),
            "title": access.project.title,
        },
        "role": access.role,
    }
    return _envelope(ctx, data)


# ================================================================================== activity
@router.get("/v1/my/projects/{project_id}/activity")
def list_my_activity(request: Request, db: _Db, ctx: _Ctx, access: _ProjectRead) -> Any:
    """Newest first: the action, when, who (a departed member as "Former member") and the ids it
    touched. Never a value (`ACTIVITY_DETAIL_SHAPES`)."""
    check_allowed(request, {"limit"})
    raw = request.query_params.get("limit", str(ACTIVITY_DEFAULT_LIMIT))
    # ASCII digits only: `"²".isdigit()` is true and `int("²")` raises, which was a 500.
    if not _ASCII_COUNT.fullmatch(raw) or not 1 <= int(raw) <= ACTIVITY_MAX_LIMIT:
        raise _invalid("limit", f"limit is between 1 and {ACTIVITY_MAX_LIMIT}")
    rows = list(
        db.execute(
            select(pm.PrivateProjectActivity)
            .where(pm.PrivateProjectActivity.project_id == access.project.id)
            .order_by(pm.PrivateProjectActivity.occurred_at.desc(), pm.PrivateProjectActivity.id.desc())
            .limit(int(raw))
        ).scalars()
    )
    members = _member_ids(db, access.project.id)
    people = _people(db, [row.actor_user_id for row in rows])
    member_public_ids = {user.public_id for user in _people(db, members).values()}
    sight = _Sight(db, ctx.entitlement)
    data = [
        {
            "action": row.action,
            "occurred_at": _when(row.occurred_at),
            "actor": _person(row.actor_user_id, people, members),
            "detail": _activity_detail(row.detail, member_public_ids, sight),
        }
        for row in rows
    ]
    return _envelope(ctx, data)


#: `ACTIVITY_DETAIL_SHAPES` keys holding a user's public id, and (type, id) pairs of public records.
_DETAIL_USER_KEYS: Final = ("user_id", "from_user_id", "to_user_id")
_DETAIL_RECORD_KEYS: Final = (("record_type", "record_public_id"), ("origin_type", "origin_public_id"))


def _activity_detail(
    detail: Mapping[str, Any] | None, member_public_ids: set[str], sight: _Sight
) -> dict[str, Any]:
    """A history row's ids as this viewer may see them: someone no longer a member is `null`, as
    their name is "Former member" (owner, 2026-10-22), and a record the viewer cannot see now is
    `null` beside its type, as on a link (lane P2 security review, 2026-10-23)."""
    out = dict(detail or {})
    for key in _DETAIL_USER_KEYS:
        if key in out and out[key] not in member_public_ids:
            out[key] = None
    for type_key, id_key in _DETAIL_RECORD_KEYS:
        if out.get(id_key) is not None:
            out[id_key] = sight.shown_id(out.get(type_key), out[id_key])
    return out


# ====================================================================================== pins
@router.get("/v1/my/pins")
def list_my_pins(request: Request, db: _Db, ctx: _Ctx, user: _User) -> Any:
    """The map's private dots (docs/19 §2.7): `{public_id, title, stage, lat, lon, precision,
    progress, role}` for each active project with a point, nothing else; the rest are counted in
    `meta.unplaced_count`. Fetched fresh on every load."""
    check_allowed(request, set())
    accesses = authz.member_projects(db, ctx)
    progress = checklist.progress_by_project(db, accesses)
    pins = []
    unplaced = 0
    for access in accesses:
        project = access.project
        if project.lat is None or project.lon is None:
            unplaced += 1
            continue
        done = progress.get(project.id, checklist.Progress(0, 0))
        pins.append(
            {
                "public_id": project.public_id,
                "title": project.title,
                "stage": project.stage,
                "lat": float(project.lat),
                "lon": float(project.lon),
                "precision": project.point_precision,
                "progress": _progress_json(done.done, done.total),
                "role": access.role,
            }
        )
    return _envelope(ctx, pins, unplaced_count=unplaced)


# ==================================================================================== export
def export_user_data(db: Session, user: User) -> dict[str, Any]:
    """**Lane P4 writes this body** (docs/19 §5.5, §8): the one JSON document
    (`schema: inframation.my-export/1`) of `user`'s own data, an `exported` activity row in each
    project they own, and nothing from a linked record but its reference and URL, and no other
    member's address. The route around it (session, `no-store`, the download headers and
    `EXPORTS_PER_DAY`) is already in place; until P4 lands it answers `503 unavailable`."""
    del db, user
    raise ProblemError(
        "unavailable",
        "Export is not available yet",
        detail="Downloading your data is coming soon; nothing was exported.",
    )


@router.get("/v1/my/export")
def export_my_data(request: Request, db: _Db, ctx: _Ctx, user: _User) -> Any:
    check_allowed(request, set())
    # A GET with a cost: `SameSite=Lax` sends the cookie on another site's top-level navigation, so
    # a link elsewhere could spend the daily exports. The browser names such a request
    # `cross-site`; it is refused before anything is spent (lane P2 security review, 2026-10-23).
    if request.headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
        raise ProblemError(
            "validation_error",
            "Cross-site request refused",
            detail="Download your data from this site.",
            errors=[{"field": "Sec-Fetch-Site", "message": "must not be cross-site"}],
        )
    _limit(f"my-export:{user.id}", EXPORTS_PER_DAY, 86_400, "exports")
    document = export_user_data(db, user)
    return JSONResponse(
        document,
        headers={"Content-Disposition": 'attachment; filename="inframation-my-data.json"'},
    )
