"""The nightly M-11 visibility audit: "publish-gate breaches: gated or restricted records visible
on any non-admin surface" (docs/10-prd-mvp.md §5, source of truth "US-906 integration test +
nightly audit query", target 0, any breach blocks release). docs/04-standards.md R-4 signs off on
"M-11 = 0 in the nightly audit", S-9 makes a breach an S1 incident, and docs/40-launch-runbook.md
§4 row 11 recorded that nothing computed it. This module is that job.

Two passes, deliberately independent of each other:

1. **Store pass** (`audit_store`). The public (anonymous) set of every surface is recomputed from
   the store through the one predicate the API uses (`services/api/visibility.py`, called, never
   re-stated), and every row in it is then checked against the *publication invariants* written
   here without the predicate: the record is `publish_state = public` and past `public_at`; its
   `min_reuse_class` is publishable under the posture in force (`services/posture.py`); every
   active source link points at a source whose licence class is publishable, whose own
   `publish_state` is `public`, and which the register (`data/sources.yaml`) does not mark
   `publication: none` / a gated `reuse` — the PJM/MISO/SPP/ISO-NE rows `CLAUDE.md` names; an
   event's own source and licence pass the same tests and its subject record is public; an asset's
   own source and licence pass; an organisation is not on the surface *only* because of gated
   evidence. A row that passes the predicate and fails an invariant is a breach. This catches the
   two ways M-11 goes wrong in practice: a predicate regression, and a store that drifted under a
   correct predicate (a licence reclassified after load while the source stayed `public`, a source
   that should never have been flipped). It also checks the **source links** the record pages
   serve (`/v1/proposals/{id}/sources`, `provenance` on every detail): docs/21 §8 item 3 says a
   gated source's link row is omitted, not greyed, so a shown record with an active link to a
   gated source is a breach on the `source_links` surface even when the record itself is rightly
   public on other evidence.
2. **Served pass** (`served_pass`). Belt and braces: real requests through the FastAPI
   `TestClient` against the real app with no credentials, for a sample of the store pass's
   breaches (does the API actually serve what the store says it would?) and for a sample of rows
   that must be hidden whatever the predicate says (taken-down and pending records, records and
   assets whose only evidence is gated). A `200` on a must-be-hidden row is a breach in its own
   right (`served_hidden`), so a serving path that bypasses the predicate is caught even when the
   predicate is right.

**Field-level provenance** (2026-10-06; QA-1 and QA-10 of the 2026-09-30 audit). A record can be
rightly public on clean evidence and still print a gated source's *values*: unpublishing EIA-860M
left its name, capacity and plant ids on merged records credited to ERCOT, and this audit did not
see it. The store pass now renders every shown record that has clean evidence and an active link
or placement on a gated source through `services/api/serialize.py` and checks, against facts
restated from the store (`field_facts`: each field whose `field_provenance` names a gated source,
no override pins it and no clean link states the same value), that no such value, no
`source_count` counting the gated link and no gated placement is printed
(`field_from_gated_source:<field>`); the served pass requests a sample of those records' detail
(`served_field_leak:<field>`). A shown record's stored link to a gated source that every surface
withholds is the routine state after an unpublish: it is reported as `withheld_links`, apart from
breaches, and is a breach only when a surface names it. Derived-only raw fields are restated too
(`raw_field_printed:<field>`, `point_coordinate_printed`; L-4). The served pass speaks as the site's
service identity (`X-Internal-Token`, the public tier unmetered), and any status other than 200/404
is inconclusive, never a leak.

**Withheld operator names** (lane E15, 2026-09-27). An asset stays public when an organisation it
names is taken down, but no non-admin surface may print that organisation's name in the asset's
register text (`services/api/withheld_names.py`; docs/00-PLAN.md 2026-09-27, lane E14). The store pass
renders every shown asset that is linked to a non-public organisation -- an `operator` edge, an
`operator_name` spelling of it, any owner edge to it, or (lane E16) an owner edge to a public
organisation whose `owner_name_raw` spells it -- through the builders the surfaces call
(`services/api/assets.py::asset_surface_shapes`: detail, list/search, map point, map line) and scans
the name-bearing parts (`operator_name`, `attributes`, `owners`) for a withheld name
(`withheld_name_printed`). The served pass requests the detail page of a sample of those assets and
searches `GET /v1/assets?q=` for a sample of the withheld spellings (`withheld_name_served`,
`withheld_name_searchable`). When every organisation is public this is one query and no breach.

**Grid interconnection points** (lane H2, 2026-09-29; docs/21 §3.24, D-16, D-17). A point is public
only when its naming register passes the source and licence tests and at least one proposal at it
is public, and every number on it is a sum over those public proposals. The store pass restates
that without the predicate: the *clean* proposals at each point (record public and past
`public_at`, class publishable, an active link to a source that is not gated) and the points whose
register is not gated and which hold one. It then renders the surfaces through the builders they
call (`services/api/interconnection_points.py`) and compares: the detail's point set
(`interconnection_point_visibility_filter`) and the list's (`listed_points`) against the clean
points (`point_shown_printed`, `point_listed_printed`); `point_totals` against the sum over clean
proposals (`point_total_printed:totals:<field>`); the proposals the detail lists
(`point_proposal_printed`) and its `recent_changes` events (`point_change_printed`); and the
proposal-detail and bulk embeds (`proposal_point_embeds`, `point_embed_printed`,
`point_total_printed:{proposal,bulk}_embed:<field>`). The served pass requests a sample: the list's
first page, a hidden point's detail and web page (must be the unknown id's 404,
`point_hidden_served`), `GET /v1/proposals?interconnection_point_id=` for it (must be the same page an
unknown id gets, `point_oracle_served`), and the busiest clean points' API detail, one proposal's
embed and the web page (`point_total_served`, `point_proposal_served`, `point_change_served`). Web
pages are requested only where the `web` package is importable (`served.web_pages`); the scheduler
image ships none. Bulk needs an API key, so its embed is checked on the store pass only. Breach
rows carry public ids and field names, never a point's name or a megawatt figure.

**Sites** (lane S2, 2026-10-10; docs/21 §3.25). A site is a parent over proposals, some of which a
caller may not see, so it is one more way a gated or restricted record could be reached. The store
pass restates each site's public members (the proposal invariants above, without the predicate) and
the groups their own stored evidence connects (`restated_groups`: a shared EIA plant id or a stored
link between two public members). Every site where something could be hidden -- a member that is
not public, a sponsor organisation that is not, a member's interconnection point that holds a
proposal that is not or whose register is gated, an anchored asset or owner that is not -- is
rendered through the builders `GET /v1/sites/{id}` calls, every group a public caller could be
served, and checked (`site_page_offences`): no member, neighbour, sponsor or anchor that is not
public (`site_member_printed`, `site_neighbour_printed`, `site_sponsor_printed`,
`site_anchor_printed:<kind>`), no group of one, and no group whose members only a hidden member
links (`site_bridge_printed`). Each public member's `site` embed, as the proposal detail and the bulk
stream render it, must name a public lead and count no more members than its restated group holds
(`site_embed_printed:<surface>:<why>`). Flagged, retired and switched-off sites must serve nothing
(`site_hidden_printed`). A site whose members are all public, with clean points and anchors, cannot
print a hidden row and is not rendered: that keeps the pass to the sites that matter. The served pass
requests, for up to `SITE_SERVED_SAMPLE` sites holding a hidden member, the API detail, one public
member's embed, the site's web page and that member's web page (which carries the "At this site"
panel), none of which may name a hidden member (`site_*_served`); and for up to as many flagged sites,
the API detail and the web page, which must be the unknown id's 404 (`site_hidden_served`).

`m11` is the total number of breaches from both passes. The result is persisted **without a new
table**: one append-only `event` row (docs/21 §3.10; subject type `source`, the closest existing
vocabulary entry for a platform-wide publication check; `event_type = visibility_audit`;
`actor_type = system`; the result dict in `after`). `services.api.audit.record_audit_event` was
the intended writer but requires a human `actor: User` and hard-codes `actor_type = "user"` —
the nightly job has no operator, and `docs/21` §3.10 gives `system` for exactly this case — so
`persist_result` writes the same row shape directly with the system actor; `GET
/admin/v1/visibility-audits` (`services/api/admin_audit_routes.py`) reads it back. The row is
never public: `published_at`/`public_at` are null and `event_visibility_filter` admits only
`proposal`/`opportunity` subjects.

Run by hand (exit 1 when `m11 > 0`, so a cron or a CI step can gate on it):

    python -m services.visibility_audit.run --posture auto [--no-persist] [--sample 25] [--json]

Scheduled: `visibility_audit_tick` in `infra/scheduler/app.py`, nightly after the daily fetch
bucket (docs/60-deployment.md §6.1), body `infra/scheduler/jobs.py::visibility_audit_tick_job`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import logging
import os
import re
import secrets
import sys
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

import sqlalchemy as sa
import yaml
from sqlalchemy import ColumnElement, exists, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session, aliased, sessionmaker

from services.api import visibility
from services.api.withheld_names import WithheldNames, withheld_names
from services.db.models import (
    Asset,
    AssetOwner,
    AssetSource,
    Event,
    InterconnectionPoint,
    Licence,
    Location,
    Opportunity,
    OpportunitySource,
    Organization,
    OrganizationAlias,
    Proposal,
    ProposalSource,
    Site,
    SiteMember,
    Source,
)
from services.db.session import get_engine, get_sessionmaker, session_scope
from services.ids import public_id
from services.posture import (
    PLATFORM_POSTURES,
    normalise_posture,
    platform_posture,
    publishable_reuse_classes,
)
from services.resolve import survivorship
from services.sites.switch import sites_enabled

logger = logging.getLogger("services.visibility_audit")

ROOT = Path(__file__).resolve().parents[2]
SOURCES_YAML = ROOT / "data" / "sources.yaml"

#: The audit row in the append-only log (module docstring): `event.subject_type` from docs/21
#: §3.10's vocabulary, a deterministic subject id so every run shares one subject (and the
#: `ix_event_subject_observed` index groups them), and an event type outside the §7.3 vocabulary,
#: named by the task and read back by the admin route. `event.event_type` carries no CHECK.
EVENT_TYPE = "visibility_audit"
SUBJECT_TYPE = "source"
SUBJECT_ID = uuid.uuid5(uuid.NAMESPACE_URL, "bankable:source:__visibility_audit__")
AUDIT_REASON = "nightly M-11 visibility audit (docs/04 R-4; a breach is an S1 incident, docs/04 S-9)"

#: The breach list persisted and returned is capped; `breach_total`/`m11` are never capped.
BREACH_CAP = 200
#: Requests issued by the served pass per candidate group (breaches, must-be-hidden rows). Kept
#: under the public tier's per-minute budget (`services/api/ratelimit.py`) with room to spare.
SERVED_SAMPLE = 25

SURFACES: tuple[str, ...] = (
    "proposals",
    "opportunities",
    "events",
    "organizations",
    "assets",
    "source_links",
    "interconnection_points",
    "sites",
)
#: Subject types an event may be served for (`event_visibility_filter`'s subject join).
_EVENT_SUBJECTS = ("proposal", "opportunity")
#: Record `publish_state` values that mean "must not be served on any non-admin surface".
HIDDEN_RECORD_STATES = ("unpublished", "pending_review")

Predicate = Callable[..., list[ColumnElement[bool]]]
#: The predicate, called by surface. Held in a dict so a test can stand in a regressed predicate
#: for one surface and prove the invariant checks catch it; production never rebinds these.
PREDICATES: dict[str, Predicate] = {
    "proposals": visibility.proposal_visibility_filter,
    "opportunities": visibility.opportunity_visibility_filter,
    "events": visibility.event_visibility_filter,
    "assets": visibility.asset_visibility_filter,
    "interconnection_points": visibility.interconnection_point_visibility_filter,
}


@dataclass
class Breach:
    surface: str
    public_id: str
    source_id: str | None
    reason: str
    #: Filled by the served pass for the sampled breaches: the HTTP status an anonymous request
    #: for the row got, and whether the response carried the offending row/link.
    served_status: int | None = None
    served_leak: bool | None = None


@dataclass(frozen=True)
class _Candidate:
    """A row that must be hidden on the public surface whatever the predicate says."""

    surface: str
    public_id: str
    why: str


@dataclass(frozen=True)
class _NameProbe:
    """A served-pass request whose response must not carry a withheld organisation name: an asset's
    detail page (`kind="detail"`, `target` its public id) or an asset search for one withheld spelling
    (`kind="search"`, `target` the spelling; `assets` the ids that name it only through register text,
    which the search must not return)."""

    kind: str
    target: str
    assets: frozenset[str] = frozenset()
    operator_edge: bool = False


# ================================================================================ the register
def register_gated_sources(posture: str, path: Path | None = None) -> dict[str, str]:
    """Source ids `data/sources.yaml` says may not be published under `posture`: `publication:
    none` or a `reuse` class outside the posture's publishable set. The register is the owner's
    statement of terms (`CLAUDE.md`; docs/21 §8 "declared, not inferred"), so a store row on one
    of these sources is a breach even when the store's own licence row disagrees. A missing or
    unreadable register yields an empty set and is reported, never raised — the store checks
    still run."""
    target = path if path is not None else SOURCES_YAML
    try:
        doc = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("register unreadable: %s", exc)
        return {}
    publishable = publishable_reuse_classes(posture)
    gated: dict[str, str] = {}
    for entry in doc.get("sources") or []:
        if not isinstance(entry, Mapping) or "id" not in entry:
            continue
        source_id = str(entry["id"])
        publication = str(entry.get("publication") or "")
        reuse = str(entry.get("reuse") or "unknown")
        if publication == "none":
            gated[source_id] = "register:publication=none"
        elif reuse not in publishable:
            gated[source_id] = f"register:reuse={reuse}"
    return gated


def gated_sources(db: Session, posture: str, register: Mapping[str, str]) -> dict[str, str]:
    """Every `source` row that may not stand behind a public row under `posture`, with the reason
    (the first that applies; all are joined with `;`): licence class not publishable, the source's
    own `publish_state` not `public`, or the register's verdict."""
    publishable = publishable_reuse_classes(posture)
    out: dict[str, str] = {}
    for source in db.scalars(select(Source)).all():
        reasons: list[str] = []
        if source.licence.reuse_class not in publishable:
            reasons.append(f"source_class_gated:{source.licence.reuse_class}")
        if source.publish_state != "public":
            reasons.append(f"source_not_public:{source.publish_state}")
        if source.id in register:
            reasons.append(register[source.id])
        if reasons:
            out[source.id] = ";".join(reasons)
    return out


def gated_licences(db: Session, posture: str) -> dict[str, str]:
    publishable = publishable_reuse_classes(posture)
    rows = db.execute(select(Licence.id, Licence.reuse_class).where(Licence.reuse_class.not_in(publishable)))
    return {lic_id: f"licence_class_gated:{cls}" for lic_id, cls in rows.all()}


# ================================================================================ store pass
def _count(db: Session, model: type[Any], where: list[ColumnElement[bool]]) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def _audit_records(
    db: Session,
    *,
    surface: str,
    model: type[Proposal] | type[Opportunity],
    link_model: type[ProposalSource] | type[OpportunitySource],
    fk: InstrumentedAttribute[uuid.UUID],
    gated_src: Mapping[str, str],
    publishable: tuple[str, ...],
    now: dt.datetime,
) -> tuple[int, list[Breach]]:
    shown = PREDICATES[surface]("public", now)
    count = _count(db, model, shown)
    breaches: list[Breach] = []

    # Record-level invariants, on the shown set. Vacuous while the predicate is right — which is
    # the point: they are the independent restatement a regression cannot share.
    rows = db.execute(
        select(model.public_id, model.publish_state, model.min_reuse_class, model.public_at).where(
            *shown,
            or_(
                model.publish_state != "public",
                model.min_reuse_class.not_in(publishable),
                model.public_at.is_(None),
                model.public_at > now,
            ),
        )
    ).all()
    for pid, state, cls, public_at in rows:
        if state != "public":
            breaches.append(Breach(surface, pid, None, f"record_not_public:{state}"))
        if cls not in publishable:
            breaches.append(Breach(surface, pid, None, f"record_class_gated:{cls}"))
        if public_at is None or _aware(public_at) > now:
            breaches.append(Breach(surface, pid, None, "record_not_yet_public"))

    # Link-level invariants: a shown record's active links to gated sources. If the record has
    # no clean link at all it is on the surface only through gated evidence (a record breach);
    # otherwise the record is legitimately public and the gated link row is the breach
    # (docs/21 §8 item 3, the mixed-provenance case).
    if gated_src:
        gated_ids = list(gated_src)
        link_rows = db.execute(
            select(model.id, model.public_id, link_model.source_id)
            .join(link_model, fk == model.id)
            .where(*shown, link_model.active.is_(True), link_model.source_id.in_(gated_ids))
        ).all()
        affected = {rid for rid, _, _ in link_rows}
        clean: set[uuid.UUID] = set()
        if affected:
            clean = set(
                db.scalars(
                    select(fk).where(
                        link_model.active.is_(True),
                        link_model.source_id.not_in(gated_ids),
                        fk.in_(list(affected)),
                    )
                ).all()
            )
        for rid, pid, sid in link_rows:
            reason = gated_src[sid]
            if rid not in clean:
                breaches.append(Breach(surface, pid, sid, f"only_gated_evidence:{reason}"))
            elif _link_rendered(db, model, rid, sid):
                breaches.append(Breach("source_links", pid, sid, f"source_link_gated:{reason}"))
            # else: the gated link is stored and correctly withheld from every surface -- the
            # routine state after an operator unpublishes a source, counted in `withheld_links`
            # by `audit_store`, never a breach (2026-10-06, QA-10 "noise").
    return count, breaches


def _link_rendered(
    db: Session, model: type[Proposal] | type[Opportunity], record_id: uuid.UUID, source_id: str
) -> bool:
    """Whether the record's public view names `source_id` anywhere a link is listed: its
    `provenance` array as `services/api/serialize.py` builds it for every record surface."""
    from services.api.serialize import serialize_opportunity, serialize_proposal

    record = cast("Proposal | Opportunity | None", db.get(model, record_id))
    if record is None:
        return False
    data = (
        serialize_proposal(record, entitlement="public")
        if isinstance(record, Proposal)
        else serialize_opportunity(record, entitlement="public")
    )
    return _mentions_source(data.get("provenance") or [], source_id)


def _withheld_link_count(db: Session, *, gated_src: Mapping[str, str], now: dt.datetime) -> int:
    """Active links from shown records to gated sources: stored, and (unless a breach says
    otherwise) withheld from every surface. Reported apart from breaches."""
    if not gated_src:
        return 0
    total = 0
    for surface, model, link_model, fk in (
        ("proposals", Proposal, ProposalSource, ProposalSource.proposal_id),
        ("opportunities", Opportunity, OpportunitySource, OpportunitySource.opportunity_id),
    ):
        total += int(
            db.scalar(
                select(func.count())
                .select_from(link_model)
                .join(model, fk == model.id)
                .where(
                    link_model.active.is_(True),
                    link_model.source_id.in_(list(gated_src)),
                    *PREDICATES[surface]("public", now),
                )
            )
            or 0
        )
    return total


# ================================================================== field-level provenance (W3)
@dataclass(frozen=True)
class _FieldFacts:
    """What the store pass established about one shown record that has both clean evidence and an
    active link (or its placement) on a gated source: the mixed-provenance case of docs/21 §8.
    Every value the record's public view may not print is listed with the gated source that
    supplied it. In memory only (it holds stored values); breach rows carry field names."""

    surface: str
    public_id: str
    gated: frozenset[str]
    #: field -> (gated source id, stored value) for each field a gated source supplied and no
    #: clean active link also states.
    gated_values: Mapping[str, tuple[str, Any]]
    #: active links whose source is not gated: the most `source_count` may say.
    clean_count: int


def _same(served: Any, stored: Any) -> bool:
    """Whether a served JSON value is the stored Python value (dates and instants as ISO text,
    numerics as floats, JSON objects as equal mappings)."""
    if served is None or stored is None:
        return False
    if isinstance(stored, dt.datetime) and isinstance(served, str):
        try:
            return _aware(dt.datetime.fromisoformat(served.replace("Z", "+00:00"))) == _aware(stored)
        except ValueError:
            return False
    if isinstance(stored, dt.date) and isinstance(served, str):
        return served[:10] == stored.isoformat()
    numeric = isinstance(stored, (int, float)) and not isinstance(stored, bool)
    if numeric or type(stored).__name__ == "Decimal":
        try:
            return float(served) == float(stored)
        except (TypeError, ValueError):
            return False
    return bool(served == stored)


def field_facts(record: Proposal | Opportunity, surface: str, gated: frozenset[str]) -> _FieldFacts:
    """`_FieldFacts` for `record`, restated from the store without the served view: a field is
    gated when its `field_provenance` names a gated source, no admin override pins it, and no
    clean active link's own `normalised` row states the same value."""
    fields = (
        visibility.PROPOSAL_SOURCED_FIELDS
        if surface == "proposals"
        else visibility.OPPORTUNITY_SOURCED_FIELDS
    )
    clean = [link for link in record.sources if link.active and link.source_id not in gated]
    overrides = record.overrides or {}
    gated_values: dict[str, tuple[str, Any]] = {}
    # What field survivorship states over the clean links alone (docs/22 §23): a merged record's
    # value can be a sum no single link states, and the served fallback is that value.
    clean_picks = (
        survivorship.survive(
            [survivorship.member_from_link(link) for link in clean if isinstance(link, ProposalSource)],
            survivorship.current_values(record),
        )
        if isinstance(record, Proposal)
        else {}
    )
    for name in fields:
        prov = (record.field_provenance or {}).get(name)
        hidden = [sid for sid in survivorship.provenance_source_ids(prov) if sid in gated]
        if name in overrides or not hidden:
            continue
        stored = getattr(record, name)
        if stored in (None, {}, []):
            continue
        if any(_same((link.normalised or {}).get(name), stored) for link in clean):
            continue
        if name in clean_picks and _same(survivorship.coerce(name, clean_picks[name].value), stored):
            continue
        gated_values[name] = (hidden[0], stored)
    return _FieldFacts(surface, record.public_id, gated, gated_values, len(clean))


def field_offences(facts: _FieldFacts, data: Mapping[str, Any]) -> list[tuple[str, str | None]]:
    """`(field, gated source)` for each place a served record (`data`, the detail shape) prints
    what `facts` says it may not: a gated source's value, a `source_count` that counts a gated
    link, or a placement whose provenance names a gated source."""
    out: list[tuple[str, str | None]] = []
    for name, (source_id, stored) in facts.gated_values.items():
        if name in data and _same(data[name], stored):
            out.append((name, source_id))
    if int(data.get("source_count") or 0) > facts.clean_count:
        out.append(("source_count", None))
    location = data.get("location") or {}
    location_source = (location.get("provenance") or {}).get("source_id")
    if location_source in facts.gated:
        out.append(("location", location_source))
    return out


def _audit_record_fields(
    db: Session, *, gated_src: Mapping[str, str], now: dt.datetime
) -> tuple[list[Breach], list[_FieldFacts]]:
    """The field-level check (docs/21 §8, the mixed-provenance case; QA-1 of the 2026-09-30
    audit): every shown record with clean evidence *and* an active link or placement on a gated
    source is rendered through the builder every record surface calls
    (`services/api/serialize.py`) and its fields checked with `field_offences`. A breach is
    `field_from_gated_source:<field>`. Returns the breaches and the facts, which the served pass
    re-checks against real responses. No gated source, no query."""
    if not gated_src:
        return [], []
    from services.api.serialize import serialize_opportunity, serialize_proposal

    gated = frozenset(gated_src)
    breaches: list[Breach] = []
    facts_out: list[_FieldFacts] = []
    clean_p = aliased(ProposalSource)
    clean_o = aliased(OpportunitySource)
    for surface, model, link_model, fk, has_clean in (
        (
            "proposals",
            Proposal,
            ProposalSource,
            ProposalSource.proposal_id,
            exists(
                select(clean_p.id).where(
                    clean_p.proposal_id == Proposal.id,
                    clean_p.active.is_(True),
                    clean_p.source_id.not_in(gated),
                )
            ),
        ),
        (
            "opportunities",
            Opportunity,
            OpportunitySource,
            OpportunitySource.opportunity_id,
            exists(
                select(clean_o.id).where(
                    clean_o.opportunity_id == Opportunity.id,
                    clean_o.active.is_(True),
                    clean_o.source_id.not_in(gated),
                )
            ),
        ),
    ):
        touches_gated = or_(
            exists(
                select(link_model.id).where(
                    fk == model.id, link_model.active.is_(True), link_model.source_id.in_(gated)
                )
            ),
            exists(
                select(Location.id).where(Location.id == model.location_id, Location.source_id.in_(gated))
            ),
        )
        stmt = select(model).where(*PREDICATES[surface]("public", now), has_clean, touches_gated)
        records = cast("list[Proposal | Opportunity]", list(db.scalars(stmt.order_by(model.id)).all()))
        for record in records:
            facts = field_facts(record, surface, gated)
            facts_out.append(facts)
            if isinstance(record, Proposal):
                data = serialize_proposal(record, entitlement="public")
            else:
                data = serialize_opportunity(record, entitlement="public")
            for name, source_id in field_offences(facts, data):
                reason = f"field_from_gated_source:{name}"
                breaches.append(Breach(surface, record.public_id, source_id, reason))
    return breaches, facts_out


def _audit_derived_only_raw(db: Session, *, now: dt.datetime) -> list[Breach]:
    """docs/21 §8's derived-only row ("no raw, no `status_raw`, no exact coordinates"; L-4 of the
    2026-09-30 legal audit): a shown record whose stored raw field (`status_raw`,
    `technology_raw`) was supplied by a source whose licence has `allows_raw_publication = false`
    must not print it, rendered through the record builder (`raw_field_printed:<field>`); and a
    shown point named by such a register must not print a coordinate typed into its name
    (`point_coordinate_printed`)."""
    from services.api.interconnection_points import point_name
    from services.api.serialize import serialize_opportunity, serialize_proposal
    from services.ingest.interconnection import COORDINATE_RUN_RE

    derived_only = {
        sid
        for sid, raw_ok in db.execute(
            select(Source.id, Licence.allows_raw_publication).join(Licence, Licence.id == Source.licence_id)
        ).all()
        if not raw_ok
    }
    breaches: list[Breach] = []
    if not derived_only:
        return breaches
    for surface, model, fields in (
        ("proposals", Proposal, ("status_raw", "technology_raw")),
        ("opportunities", Opportunity, ("status_raw",)),
    ):
        has_raw = or_(*(getattr(model, f).is_not(None) for f in fields))
        stmt = select(model).where(*PREDICATES[surface]("public", now), has_raw).order_by(model.id)
        for record in cast("list[Proposal | Opportunity]", list(db.scalars(stmt).all())):
            provenance = record.field_provenance or {}
            suspect = [
                f
                for f in fields
                if f not in (record.overrides or {})
                and isinstance(provenance.get(f), dict)
                and provenance[f].get("source_id") in derived_only
            ]
            if not suspect:
                continue
            data = (
                serialize_proposal(record, entitlement="public")
                if isinstance(record, Proposal)
                else serialize_opportunity(record, entitlement="public")
            )
            for f in suspect:
                if data.get(f) is not None:
                    breaches.append(
                        Breach(
                            surface, record.public_id, provenance[f]["source_id"], f"raw_field_printed:{f}"
                        )
                    )
    points = db.scalars(
        select(InterconnectionPoint).where(
            *PREDICATES[POINTS]("public", now), InterconnectionPoint.source_id.in_(sorted(derived_only))
        )
    ).all()
    for point in points:
        if COORDINATE_RUN_RE.search(point_name(point)):
            breaches.append(Breach(POINTS, point.public_id, point.source_id, "point_coordinate_printed"))
    return breaches


def _audit_events(
    db: Session,
    *,
    gated_src: Mapping[str, str],
    gated_lic: Mapping[str, str],
    now: dt.datetime,
) -> tuple[int, list[Breach]]:
    shown = PREDICATES["events"]("public", now)
    count = _count(db, Event, shown)
    src_clause = Event.source_id.in_(list(gated_src)) if gated_src else sa.false()
    lic_clause = Event.licence_id.in_(list(gated_lic)) if gated_lic else sa.false()
    subject_hidden = or_(
        Event.subject_type.not_in(_EVENT_SUBJECTS),
        exists(
            select(Proposal.id).where(
                Event.subject_type == "proposal",
                Proposal.id == Event.subject_id,
                Proposal.publish_state != "public",
            )
        ),
        exists(
            select(Opportunity.id).where(
                Event.subject_type == "opportunity",
                Opportunity.id == Event.subject_id,
                Opportunity.publish_state != "public",
            )
        ),
    )
    rows = db.execute(
        select(
            Event.id, Event.subject_type, Event.subject_id, Event.source_id, Event.licence_id, Event.public_at
        ).where(
            *shown,
            or_(src_clause, lic_clause, subject_hidden, Event.public_at.is_(None), Event.public_at > now),
        )
    ).all()
    breaches: list[Breach] = []
    for eid, subject_type, subject_id, source_id, licence_id, public_at in rows:
        pid = public_id("evt", eid)
        if source_id in gated_src:
            breaches.append(Breach("events", pid, source_id, f"event_source_gated:{gated_src[source_id]}"))
        if licence_id in gated_lic:
            breaches.append(Breach("events", pid, source_id, f"event_{gated_lic[licence_id]}"))
        if subject_type not in _EVENT_SUBJECTS:
            breaches.append(Breach("events", pid, source_id, f"event_subject_type:{subject_type}"))
        else:
            state = _subject_publish_state(db, subject_type, subject_id)
            if state is not None and state != "public":
                breaches.append(Breach("events", pid, source_id, f"event_subject_not_public:{state}"))
        if public_at is None or _aware(public_at) > now:
            breaches.append(Breach("events", pid, source_id, "event_not_yet_public"))
    return count, breaches


def _subject_publish_state(db: Session, subject_type: str, subject_id: uuid.UUID) -> str | None:
    if subject_type == "proposal":
        proposal = db.get(Proposal, subject_id)
        return proposal.publish_state if proposal is not None else None
    opportunity = db.get(Opportunity, subject_id)
    return opportunity.publish_state if opportunity is not None else None


def _audit_assets(
    db: Session,
    *,
    gated_src: Mapping[str, str],
    gated_lic: Mapping[str, str],
    now: dt.datetime,
) -> tuple[int, list[Breach]]:
    shown = PREDICATES["assets"]("public", now)
    count = _count(db, Asset, shown)
    breaches: list[Breach] = []
    src_clause = Asset.source_id.in_(list(gated_src)) if gated_src else sa.false()
    lic_clause = Asset.licence_id.in_(list(gated_lic)) if gated_lic else sa.false()
    rows = db.execute(
        select(Asset.public_id, Asset.source_id, Asset.licence_id).where(*shown, or_(src_clause, lic_clause))
    ).all()
    for pid, source_id, licence_id in rows:
        if source_id in gated_src:
            breaches.append(Breach("assets", pid, source_id, f"asset_source_gated:{gated_src[source_id]}"))
        if licence_id in gated_lic:
            breaches.append(Breach("assets", pid, source_id, f"asset_{gated_lic[licence_id]}"))
    if gated_src:
        link_rows = db.execute(
            select(Asset.public_id, Asset.source_id, AssetSource.source_id)
            .join(AssetSource, AssetSource.asset_id == Asset.id)
            .where(*shown, AssetSource.source_id.in_(list(gated_src)))
        ).all()
        for pid, own_source, link_source in link_rows:
            if link_source == own_source:
                continue  # already a breach on the asset itself above
            breaches.append(
                Breach("source_links", pid, link_source, f"source_link_gated:{gated_src[link_source]}")
            )
    return count, breaches


def withheld_name_paths(
    shape: Mapping[str, Any],
    withheld: WithheldNames,
    *,
    operator_edge: bool,
    hidden_org_ids: frozenset[str] = frozenset(),
) -> list[str]:
    """The paths in one rendered asset shape (a JSON object, or a GeoJSON feature's `properties`) that
    print a withheld organisation. Only the parts that carry register text or organisation edges are
    read (`operator_name`, `attributes`, `owners`); the asset's own name, provenance and geometry are
    not about the organisation. A hit is any string in `operator_name`/`attributes` whose `org_key` is a
    withheld key; an `owners[]` edge to a hidden organisation (by public id -- a *public* owner that
    happens to share a spelling is that owner, printed rightly); any raw string on an `owners[]` edge
    (`owner_name_raw`, the register's spelling) whose `org_key` is a withheld key, even on an edge to a
    public organisation (lane E16: the edge's `organization` summary and `provenance` are not raw text
    and are not read); and, where the asset has an `operator` edge to a hidden organisation, any
    `operator_name` at all (the register may spell the company in a way no key catches, which is why
    the edge withholds it)."""
    body: Mapping[str, Any] = shape.get("properties", shape) if shape.get("type") == "Feature" else shape
    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, str):
            if withheld.names_withheld(node):
                found.append(path)
        elif isinstance(node, Mapping):
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    for key in ("operator_name", "attributes"):
        if key in body:
            walk(body[key], key)
    for i, edge in enumerate(body.get("owners") or []):
        if not isinstance(edge, Mapping):
            continue
        org = edge.get("organization")
        if isinstance(org, Mapping) and org.get("public_id") in hidden_org_ids:
            found.append(f"owners[{i}]")
            continue
        for key, value in edge.items():
            if key not in ("organization", "provenance"):
                walk(value, f"owners[{i}].{key}")
    if operator_edge and body.get("operator_name") is not None and "operator_name" not in found:
        found.append("operator_name")
    return found


def _hidden_organization_ids(db: Session) -> tuple[list[uuid.UUID], frozenset[str]]:
    rows = db.execute(
        select(Organization.id, Organization.public_id).where(Organization.publish_state != "public")
    )
    pairs = rows.all()
    return [r[0] for r in pairs], frozenset(r[1] for r in pairs)


def _audit_asset_operator_names(
    db: Session,
    *,
    now: dt.datetime,
    withheld: WithheldNames,
    hidden: tuple[list[uuid.UUID], frozenset[str]],
) -> tuple[list[Breach], list[_NameProbe]]:
    """The store half of the withheld-name check (module docstring). `hidden` is
    `_hidden_organization_ids`. Returns the breaches and the served-pass probes: every linked asset's
    detail page, and one search per withheld spelling."""
    if withheld.empty:
        return [], []
    from sqlalchemy.orm import selectinload

    from services.api.assets import asset_surface_shapes

    hidden_ids, hidden_public_ids = hidden
    # Lane E16: an edge to a *public* organisation whose raw spelling keys to a withheld one also links
    # the asset (`withheld_name_paths` reads `owners[].owner_name_raw`). The distinct raw spellings are
    # a few thousand strings on the dev store, keyed through the same memoised `org_key`.
    owner_spellings = sorted(
        name
        for name in db.scalars(select(AssetOwner.owner_name_raw).distinct())
        if withheld.names_withheld(name)
    )
    linked = or_(
        ~withheld.operator_name_searchable(),
        Asset.id.in_(select(AssetOwner.asset_id).where(AssetOwner.organization_id.in_(hidden_ids))),
        Asset.id.in_(select(AssetOwner.asset_id).where(AssetOwner.owner_name_raw.in_(owner_spellings))),
    )
    assets = list(
        db.scalars(
            select(Asset)
            .where(*PREDICATES["assets"]("public", now), linked)
            .options(selectinload(Asset.owners).selectinload(AssetOwner.organization))
            .order_by(Asset.public_id)
        ).all()
    )
    shapes = asset_surface_shapes(db, assets, withheld)
    breaches: list[Breach] = []
    for asset in assets:
        edge = asset.id in withheld.operator_edge_asset_ids
        for shape, rendered in shapes[asset.public_id]:
            paths = withheld_name_paths(
                rendered, withheld, operator_edge=edge, hidden_org_ids=hidden_public_ids
            )
            if paths:
                breaches.append(
                    Breach(
                        "assets",
                        asset.public_id,
                        asset.source_id,
                        f"withheld_name_printed:{shape}:{paths[0]}",
                    )
                )
    probes = [
        _NameProbe("detail", a.public_id, operator_edge=a.id in withheld.operator_edge_asset_ids)
        for a in assets
    ]
    for spelling in sorted(withheld.operator_spellings):
        # An asset the search may rightly return for this text: its own name contains it, or a
        # *visible* owner's name does (`_asset_query_with_filters`' other two arms). Any other
        # linked asset whose operator name is withheld must not come back.
        needle = spelling.lower()
        must_not_match = frozenset(
            a.public_id
            for a in assets
            if withheld.operator_withheld(a.id, a.operator_name)
            and needle not in (a.name or "").lower()
            and not any(
                visibility.organization_visible(o.organization)
                and needle in o.organization.name_canonical.lower()
                for o in a.owners
            )
        )
        probes.append(_NameProbe("search", spelling, must_not_match))
    return breaches, probes


def _audit_organizations(
    db: Session, *, gated_src: Mapping[str, str], now: dt.datetime
) -> tuple[int, list[Breach]]:
    """`GET /v1/organizations` serves every unmerged organisation (it has no licence of its own,
    docs/21 §3.5), so the surface check is existence-based: an organisation with no visible
    proposal, opportunity or asset edge, but with at least one edge to a gated source, is on the
    surface only because of gated evidence — the existence disclosure docs/21 §8 item 3 forbids.
    "Visible" here means shown by the predicate *and* resting on at least one clean link, so an
    organisation whose only shown record is itself a gated-evidence breach is counted too."""
    # What `GET /v1/organizations` actually serves: unmerged rows the organisation arm of the
    # predicate admits (its evidence clause hides an organisation every alias of which comes from a
    # gated source, 2026-10-06). Until then this set was every unmerged row, so a correctly hidden
    # organisation still counted as a breach.
    served: list[ColumnElement[bool]] = [
        Organization.merged_into_id.is_(None),
        *visibility.organization_visibility_filter("public", now),
    ]
    count = _count(db, Organization, served)
    if not gated_src:
        return count, []
    gated_ids = list(gated_src)
    clean_p = aliased(ProposalSource)
    clean_o = aliased(OpportunitySource)
    visible = or_(
        # A spelling a clean source states, or Infraque's own curated statement, is evidence of
        # the organisation's existence that discloses nothing of a gated register (2026-10-06).
        Organization.is_curated_issuer.is_(True),
        exists(
            select(OrganizationAlias.id).where(
                OrganizationAlias.organization_id == Organization.id,
                OrganizationAlias.source_id.not_in(gated_ids),
            )
        ),
        exists(
            select(Proposal.id).where(
                Proposal.sponsor_org_id == Organization.id,
                *PREDICATES["proposals"]("public", now),
                exists(
                    select(clean_p.id).where(
                        clean_p.proposal_id == Proposal.id,
                        clean_p.active.is_(True),
                        clean_p.source_id.not_in(gated_ids),
                    )
                ),
            )
        ),
        exists(
            select(Opportunity.id).where(
                Opportunity.issuer_org_id == Organization.id,
                *PREDICATES["opportunities"]("public", now),
                exists(
                    select(clean_o.id).where(
                        clean_o.opportunity_id == Opportunity.id,
                        clean_o.active.is_(True),
                        clean_o.source_id.not_in(gated_ids),
                    )
                ),
            )
        ),
        exists(
            select(AssetOwner.id)
            .join(Asset, Asset.id == AssetOwner.asset_id)
            .where(
                AssetOwner.organization_id == Organization.id,
                AssetOwner.source_id.not_in(gated_ids),
                Asset.source_id.not_in(gated_ids),
                *PREDICATES["assets"]("public", now),
            )
        ),
    )
    gated_edge = or_(
        exists(
            select(ProposalSource.id)
            .join(Proposal, Proposal.id == ProposalSource.proposal_id)
            .where(Proposal.sponsor_org_id == Organization.id, ProposalSource.source_id.in_(gated_ids))
        ),
        exists(
            select(OpportunitySource.id)
            .join(Opportunity, Opportunity.id == OpportunitySource.opportunity_id)
            .where(Opportunity.issuer_org_id == Organization.id, OpportunitySource.source_id.in_(gated_ids))
        ),
        exists(
            select(AssetOwner.id).where(
                AssetOwner.organization_id == Organization.id, AssetOwner.source_id.in_(gated_ids)
            )
        ),
        exists(
            select(OrganizationAlias.id).where(
                OrganizationAlias.organization_id == Organization.id,
                OrganizationAlias.source_id.in_(gated_ids),
            )
        ),
    )
    rows = db.execute(
        select(Organization.id, Organization.public_id).where(*served, gated_edge, ~visible)
    ).all()
    breaches: list[Breach] = []
    for oid, pid in rows:
        sid = _first_gated_org_source(db, oid, gated_ids)
        reason = gated_src.get(sid or "", "gated")
        breaches.append(Breach("organizations", pid, sid, f"organization_gated_evidence_only:{reason}"))
    return count, breaches


def _first_gated_org_source(db: Session, org_id: uuid.UUID, gated_ids: list[str]) -> str | None:
    for stmt in (
        select(ProposalSource.source_id)
        .join(Proposal, Proposal.id == ProposalSource.proposal_id)
        .where(Proposal.sponsor_org_id == org_id, ProposalSource.source_id.in_(gated_ids)),
        select(OpportunitySource.source_id)
        .join(Opportunity, Opportunity.id == OpportunitySource.opportunity_id)
        .where(Opportunity.issuer_org_id == org_id, OpportunitySource.source_id.in_(gated_ids)),
        select(AssetOwner.source_id).where(
            AssetOwner.organization_id == org_id, AssetOwner.source_id.in_(gated_ids)
        ),
        select(OrganizationAlias.source_id).where(
            OrganizationAlias.organization_id == org_id, OrganizationAlias.source_id.in_(gated_ids)
        ),
    ):
        found = db.scalar(stmt.limit(1))
        if found is not None:
            return str(found)
    return None


# ================================================================== interconnection points (H2)
POINTS = "interconnection_points"
#: Anonymous requests the served pass spends on points, per group (hidden points, busiest clean
#: points). A group costs two anonymous requests per point (the web page goes through the site's own
#: service identity and costs none), plus one list page and one unknown-id baseline -- 22 at the
#: default, inside the public tier's hourly 60 (`services/api/ratelimit.py`) with the other groups.
POINT_SERVED_SAMPLE = 5
#: What the `interconnection_point_id=` filter must answer for a hidden point: this id's page.
UNKNOWN_POINT_ID = "poi_0000000000"
#: The scalar totals compared, and the embed's subset. MW fields are compared within
#: `MW_TOLERANCE`: the API rounds to 3 places and SQL sums in a different order from Python.
POINT_TOTAL_FIELDS: tuple[str, ...] = (
    "proposal_count",
    "active_count",
    "active_mw",
    "withdrawn_count",
    "withdrawn_mw",
    "built_count",
    "built_mw",
    "other_count",
    "other_mw",
)
EMBED_TOTAL_FIELDS: tuple[str, ...] = ("active_mw", "active_count", "proposal_count")
MW_TOLERANCE = 0.0015
_POINT_CHUNK = 500


@dataclass
class _PointFacts:
    """What the store pass established about points, for the served pass. In memory only: it holds
    expected totals, which are never persisted."""

    #: clean point public id -> expected scalar totals over its clean proposals.
    expected: dict[str, dict[str, Any]]
    #: clean point public id -> the public ids of its clean proposals.
    clean_proposals: dict[str, frozenset[str]]
    #: stored points that must not exist publicly, `(public_id, why)`, by public id.
    hidden: list[tuple[str, str]]
    #: whether the store holds any point at all (none: no point probe, no request spent).
    any_points: bool
    #: sources no served change event may credit, and the run's instant (an event must be public by it).
    gated_sources: frozenset[str] = frozenset()
    now: dt.datetime | None = None


def _chunks(items: list[Any], size: int = _POINT_CHUNK) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def first_total_mismatch(
    got: Mapping[str, Any], want: Mapping[str, Any], fields: tuple[str, ...]
) -> str | None:
    """The first of `fields` on which a served or rendered total disagrees with the expected one
    (a missing field disagrees), or `None`."""
    for name in fields:
        value, expected = got.get(name), want.get(name)
        if value is None or expected is None or abs(float(value) - float(expected)) > MW_TOLERANCE:
            return name
    return None


def _clean_field_value(
    db: Session,
    proposal_id: uuid.UUID,
    name: str,
    stored: Any,
    provenance: Mapping[str, Any] | None,
    overrides: Mapping[str, Any] | None,
    gated_src: Mapping[str, str],
) -> Any:
    """The value of one proposal field that a public sum may count, restated from the store
    without the served view: the stored value unless its `field_provenance` names a gated source
    (or names none while the record has an active gated link) and no admin override pins it, in
    which case what field survivorship states over the clean active links alone (docs/22 §23),
    else `None`."""
    named = survivorship.provenance_source_ids((provenance or {}).get(name))
    if name in (overrides or {}):
        return stored
    if named and not any(sid in gated_src for sid in named):
        return stored
    links = db.scalars(
        select(ProposalSource).where(
            ProposalSource.proposal_id == proposal_id, ProposalSource.active.is_(True)
        )
    ).all()
    if not named and not any(link.source_id in gated_src for link in links):
        return stored
    # The served fallback's rule (docs/22 §23): field survivorship over the clean links alone.
    clean = [survivorship.member_from_link(link) for link in links if link.source_id not in gated_src]
    pick = survivorship.survive(clean).get(name)
    return pick.value if pick is not None else None


def _audit_interconnection_points(
    db: Session,
    *,
    gated_src: Mapping[str, str],
    gated_lic: Mapping[str, str],
    publishable: tuple[str, ...],
    now: dt.datetime,
) -> tuple[int, list[Breach], _PointFacts]:
    """The store half of the point checks (module docstring). Returns the detail surface's shown
    count, the breaches and the facts the served pass compares against."""
    from services.api import interconnection_points as ip

    meta = {
        row[0]: (row[1], row[2], row[3])
        for row in db.execute(
            select(
                InterconnectionPoint.id,
                InterconnectionPoint.public_id,
                InterconnectionPoint.source_id,
                InterconnectionPoint.licence_id,
            )
        ).all()
    }
    if not meta:
        return 0, [], _PointFacts({}, {}, [], any_points=False)

    # The clean proposals at each point, restated without the predicate (module docstring).
    link = aliased(ProposalSource)
    clean_link = [link.proposal_id == Proposal.id, link.active.is_(True)]
    if gated_src:
        clean_link.append(link.source_id.not_in(list(gated_src)))
    rows = db.execute(
        select(
            Proposal.id,
            Proposal.public_id,
            Proposal.interconnection_point_id,
            Proposal.lifecycle_state,
            Proposal.technology,
            Proposal.capacity_mw,
            Proposal.field_provenance,
            Proposal.overrides,
        ).where(
            Proposal.interconnection_point_id.is_not(None),
            Proposal.publish_state == "public",
            Proposal.public_at.is_not(None),
            Proposal.public_at <= now,
            Proposal.min_reuse_class.in_(publishable),
            exists(select(link.id).where(*clean_link)),
        )
    ).all()
    totals: dict[uuid.UUID, Any] = {}
    clean_props: dict[uuid.UUID, set[uuid.UUID]] = {}
    clean_prop_public: dict[uuid.UUID, set[str]] = {}
    for prop_id, prop_public, point_id, state, technology, mw, provenance, overrides in rows:
        # docs/21 §8 item 4: a sum includes no gated source's value. A field a gated source
        # supplied counts at what the record's clean links state (`_clean_field_value`).
        if gated_src:
            state, technology, mw = (
                _clean_field_value(db, prop_id, name, stored, provenance, overrides, gated_src)
                for name, stored in (
                    ("lifecycle_state", state),
                    ("technology", technology),
                    ("capacity_mw", mw),
                )
            )
            state = state or "unknown"
        totals.setdefault(point_id, ip.PointTotals()).add(state, technology, 1, float(mw or 0))
        clean_props.setdefault(point_id, set()).add(prop_id)
        clean_prop_public.setdefault(point_id, set()).add(prop_public)

    def why(point_id: uuid.UUID) -> str:
        _public, source_id, licence_id = meta[point_id]
        if source_id in gated_src:
            return f"register_gated:{gated_src[source_id]}"
        if licence_id in gated_lic:
            return gated_lic[licence_id]
        return "no_visible_proposal"

    def gated_source(point_id: uuid.UUID) -> str | None:
        source_id = meta[point_id][1]
        return source_id if source_id in gated_src else None

    clean = {
        pid
        for pid in totals
        if pid in meta and meta[pid][1] not in gated_src and meta[pid][2] not in gated_lic
    }
    breaches: list[Breach] = []

    # The two ways a point reaches a page: the detail's predicate and the list's row set.
    shown = set(db.scalars(select(InterconnectionPoint.id).where(*PREDICATES[POINTS]("public", now))).all())
    listed_stmt, _agg = ip.listed_points("public")
    listed_sub = listed_stmt.subquery()
    listed = set(db.scalars(select(listed_sub.c.id)).all())
    for label, surfaced in (("point_shown_printed", shown), ("point_listed_printed", listed)):
        for pid in sorted(surfaced - clean, key=lambda i: meta[i][0]):
            breaches.append(Breach(POINTS, meta[pid][0], gated_source(pid), f"{label}:{why(pid)}"))

    rendered_ids = sorted(clean | shown | listed, key=str)
    # Totals, through the builder the list, the detail and the embeds call.
    rendered: dict[uuid.UUID, Any] = {}
    listed_props: dict[uuid.UUID, set[uuid.UUID]] = {}
    changes: dict[uuid.UUID, list[Event]] = {}
    for chunk in _chunks(rendered_ids):
        rendered.update(ip.point_totals(db, chunk, "public", now))
        for point_id, prop_id in db.execute(
            select(Proposal.interconnection_point_id, Proposal.id).where(
                Proposal.interconnection_point_id.in_(chunk), *ip.point_proposal_filter("public", now)
            )
        ).all():
            listed_props.setdefault(point_id, set()).add(prop_id)
        changes.update(ip.recent_point_changes(db, chunk, "public", now=now))
    for pid in sorted(clean, key=lambda i: meta[i][0]):
        got = rendered.get(pid, ip.PointTotals()).as_dict()
        field_name = first_total_mismatch(got, totals[pid].as_dict(), POINT_TOTAL_FIELDS)
        if field_name:
            breaches.append(Breach(POINTS, meta[pid][0], None, f"point_total_printed:totals:{field_name}"))
    for pid in sorted(listed_props, key=lambda i: meta[i][0]):
        if listed_props[pid] - clean_props.get(pid, set()):
            breaches.append(Breach(POINTS, meta[pid][0], None, "point_proposal_printed:detail"))
    for pid in sorted(changes, key=lambda i: meta[i][0]):
        reason = _change_offence(changes[pid], clean_props.get(pid, set()), gated_src, gated_lic, now)
        if reason:
            breaches.append(Breach(POINTS, meta[pid][0], None, f"point_change_printed:{reason}"))

    breaches.extend(
        _audit_point_embeds(
            db, ip=ip, meta=meta, clean=clean, totals=totals, why=why, gated_source=gated_source, now=now
        )
    )

    by_public = {meta[pid][0]: pid for pid in clean}
    facts = _PointFacts(
        expected={pub: _scalar_totals(totals[pid].as_dict()) for pub, pid in by_public.items()},
        clean_proposals={pub: frozenset(clean_prop_public[pid]) for pub, pid in by_public.items()},
        hidden=sorted((meta[pid][0], why(pid)) for pid in meta if pid not in clean),
        any_points=True,
        gated_sources=frozenset(gated_src),
        now=now,
    )
    return len(shown), breaches, facts


def _scalar_totals(totals: Mapping[str, Any]) -> dict[str, Any]:
    return {name: totals[name] for name in POINT_TOTAL_FIELDS}


def _change_offence(
    events: list[Event],
    clean_props: set[uuid.UUID],
    gated_src: Mapping[str, str],
    gated_lic: Mapping[str, str],
    now: dt.datetime,
) -> str | None:
    """Why one point's rendered `recent_changes` breaches, or `None`: an event whose own source or
    licence is gated, that is not yet public, or whose subject is not a clean proposal at the
    point."""
    for event in events:
        if event.source_id in gated_src:
            return "event_source_gated"
        if event.licence_id in gated_lic:
            return "event_licence_gated"
        if event.public_at is None or _aware(event.public_at) > now:
            return "event_not_yet_public"
        if event.subject_type != "proposal" or event.subject_id not in clean_props:
            return "subject_not_visible"
    return None


def _audit_point_embeds(
    db: Session,
    *,
    ip: Any,
    meta: Mapping[uuid.UUID, tuple[str, str, str]],
    clean: set[uuid.UUID],
    totals: Mapping[uuid.UUID, Any],
    why: Callable[[uuid.UUID], str],
    gated_source: Callable[[uuid.UUID], str | None],
    now: dt.datetime,
) -> list[Breach]:
    """The `interconnection_point` embed on every shown proposal that links a point, rendered
    through `proposal_point_embeds` as the proposal detail calls it and as bulk calls it
    (`redistribution=True`). The embed must name a clean point with the clean totals; bulk's must
    also be absent when the point's licence forbids API redistribution. Bulk is rendered at the
    public tier: the invariants here are the public tier's."""
    from sqlalchemy.orm import load_only

    no_redistribution = set(
        db.scalars(select(Licence.id).where(Licence.allows_api_redistribution.is_(False))).all()
    )
    by_public = {value[0]: key for key, value in meta.items()}
    proposals = list(
        db.scalars(
            select(Proposal)
            .options(load_only(Proposal.id, Proposal.public_id, Proposal.interconnection_point_id))
            .where(Proposal.interconnection_point_id.is_not(None), *PREDICATES["proposals"]("public", now))
            .order_by(Proposal.public_id)
        ).all()
    )
    breaches: list[Breach] = []
    for chunk in _chunks(proposals):
        for surface, redistribution in (("proposal_embed", False), ("bulk_embed", True)):
            embeds = ip.proposal_point_embeds(db, chunk, "public", redistribution=redistribution)
            for proposal in chunk:
                embed = embeds.get(proposal.id)
                if not embed:
                    continue
                point_id = by_public.get(str(embed.get("public_id")))
                if point_id is None or point_id not in clean:
                    reason = why(point_id) if point_id is not None else "unknown_point"
                    source = gated_source(point_id) if point_id is not None else None
                    breaches.append(
                        Breach(POINTS, proposal.public_id, source, f"point_embed_printed:{surface}:{reason}")
                    )
                elif redistribution and meta[point_id][2] in no_redistribution:
                    breaches.append(
                        Breach(
                            POINTS,
                            proposal.public_id,
                            None,
                            f"point_embed_printed:{surface}:licence_no_api_redistribution",
                        )
                    )
                elif field_name := first_total_mismatch(
                    embed, totals[point_id].as_dict(), EMBED_TOTAL_FIELDS
                ):
                    breaches.append(
                        Breach(
                            POINTS, proposal.public_id, None, f"point_total_printed:{surface}:{field_name}"
                        )
                    )
    return breaches


# ===================================================================================== sites (S2)
SITES = "sites"
#: Sites the served pass requests per group: servable sites that hold a member that is not public,
#: and sites flagged for review. A probed site costs two API requests (its detail, one member's
#: embed) and two web pages (the site page, that member's page with its "At this site" panel); a
#: flagged one costs one of each. Every request speaks as the site's service identity, unmetered.
SITE_SERVED_SAMPLE = 5


@dataclass
class _SiteFacts:
    """What the store pass established about sites, for the served pass. In memory only."""

    #: Public ids of the proposals any public page may print, restated without the predicate.
    clean: frozenset[str]
    #: Member public id -> its EIA plant ids, and -> the members its stored links name.
    plants: dict[str, frozenset[str]]
    links: dict[str, frozenset[str]]
    #: Site public id -> its members as (public id, slug): those not clean, and those clean.
    hidden_members: dict[str, list[tuple[str, str]]]
    clean_members: dict[str, list[tuple[str, str]]]
    #: Servable sites holding a member that is not clean, by public id: what the served pass probes.
    probe: list[str]
    #: Sites no page may serve, `(public id, why)`.
    hidden: list[tuple[str, str]]
    #: Public member public id -> the size of its group over public members, restated (0 when no
    #: page may show it a site: a group of one, or a site that must serve nothing).
    expected: dict[str, int]
    #: Anchors a page may print (asset and point public ids), and organisations it may not.
    clean_assets: frozenset[str] = frozenset()
    clean_points: frozenset[str] = frozenset()
    hidden_orgs: frozenset[str] = frozenset()


def _stored_partners(evidence: Mapping[str, Any] | None) -> frozenset[str]:
    links = (evidence or {}).get("links")
    return frozenset(str(k) for k in links) if isinstance(links, dict | list) else frozenset()


def restated_groups(
    members: Iterable[str], plants: Mapping[str, frozenset[str]], links: Mapping[str, frozenset[str]]
) -> dict[str, str]:
    """`member -> group root` over `members` alone: two are joined when they share an EIA plant id or
    a stored link between the two names the other. Restated here (a walk over the pairs), not taken
    from `services/sites/rules.py`, so a regression there cannot hide itself."""
    pool = set(members)
    near: dict[str, set[str]] = {m: set() for m in pool}
    holders: dict[str, list[str]] = defaultdict(list)
    for m in sorted(pool):
        for other in links.get(m, frozenset()):
            if other in pool and other != m:
                near[m].add(other)
                near[other].add(m)
        for plant in plants.get(m, frozenset()):
            holders[plant].append(m)
    for group in holders.values():
        for a, b in itertools.pairwise(group):
            near[a].add(b)
            near[b].add(a)
    root: dict[str, str] = {}
    for start in sorted(pool):
        if start in root:
            continue
        stack = [start]
        root[start] = start
        while stack:
            for nxt in near[stack.pop()]:
                if nxt not in root:
                    root[nxt] = start
                    stack.append(nxt)
    return root


def site_page_offences(data: Mapping[str, Any], facts: _SiteFacts) -> list[str]:
    """Why one served site (`GET /v1/sites/{id}`'s `data`, or the store pass's render of it)
    breaches: a member, neighbour, sponsor or anchor that is not public, a group of fewer than two,
    or members that only a hidden member links (`site_bridge_printed`). Empty when it does not."""
    out: list[str] = []
    rows = list(data.get("members") or [])
    members = [str(m.get("public_id")) for m in rows]
    if len(members) < 2:
        out.append("site_singleton_printed")
    if any(m not in facts.clean for m in members):
        out.append("site_member_printed:not_visible")
    clean = [m for m in members if m in facts.clean]
    if len(set(restated_groups(clean, facts.plants, facts.links).values())) > 1:
        out.append("site_bridge_printed:linked_only_through_a_hidden_member")
    neighbours = data.get("shares_interconnection_point") or []
    if any(str(n.get("public_id")) not in facts.clean for n in neighbours):
        out.append("site_neighbour_printed:not_visible")
    sponsors = [*(m.get("sponsor") for m in rows), *(data.get("sponsors") or [])]
    if any(s and s.get("public_id") in facts.hidden_orgs for s in sponsors):
        out.append("site_sponsor_printed:not_visible")
    anchors = data.get("anchors") or {}
    plants = {p for m in clean for p in facts.plants.get(m, frozenset())}
    if not {str(p) for p in anchors.get("eia_plant_ids") or []} <= plants:
        out.append("site_anchor_printed:eia_plant_id")
    assets = anchors.get("assets") or []
    if any(str(a.get("public_id")) not in facts.clean_assets for a in assets):
        out.append("site_anchor_printed:asset")
    owners = [(o.get("organization") or {}).get("public_id") for a in assets for o in a.get("owners") or []]
    if any(o in facts.hidden_orgs for o in owners):
        out.append("site_anchor_printed:owner")
    if any(
        str(pt.get("public_id")) not in facts.clean_points
        for pt in anchors.get("interconnection_points") or []
    ):
        out.append("site_anchor_printed:point")
    return out


def site_embed_offence(embed: Mapping[str, Any] | None, allowed: int, facts: _SiteFacts) -> str | None:
    """Why a proposal's `site` embed breaches, or `None`: its lead is not public, or it counts more
    members than the record's group over public members holds (`allowed`; 0 when no public page may
    show the record a site), which is what a bridge through a hidden member or a site that must not be
    served looks like."""
    if not embed:
        return None
    if str((embed.get("lead") or {}).get("public_id")) not in facts.clean:
        return "lead_not_visible"
    count = int(embed.get("member_count") or 0)
    if count > allowed:
        return "member_count_over_visible_group" if allowed else "site_not_servable"
    return None


def _audit_sites(
    db: Session,
    *,
    gated_src: Mapping[str, str],
    gated_lic: Mapping[str, str],
    publishable: tuple[str, ...],
    now: dt.datetime,
) -> tuple[int, list[Breach], _SiteFacts | None]:
    """The store half of the site checks (module docstring). Every site is restated from the store:
    its members that are public (the proposals invariants, without the predicate) and the groups
    their own stored evidence connects. Sites where something may be hidden -- a member that is not
    public, a sponsor organisation that is not, a neighbour at a member's point that is not, an
    anchored asset or point on a gated register -- are rendered through the builders the surfaces
    call (`services/api/sites.py`: the detail and the proposal and bulk embeds) and compared. A site
    whose members are all public, with clean neighbours and anchors, cannot print a hidden row and
    is not rendered. Flagged and retired sites must serve nothing."""
    from services.api import sites as sites_api

    site_rows = list(db.scalars(select(Site)).all())
    if not site_rows:
        return 0, [], None
    link = aliased(ProposalSource)
    clean_link = [link.proposal_id == Proposal.id, link.active.is_(True)]
    if gated_src:
        clean_link.append(link.source_id.not_in(list(gated_src)))
    clean_ids = {
        row[0]: row[1]
        for row in db.execute(
            select(Proposal.id, Proposal.public_id).where(
                Proposal.publish_state == "public",
                Proposal.public_at.is_not(None),
                Proposal.public_at <= now,
                Proposal.min_reuse_class.in_(publishable),
                exists(select(link.id).where(*clean_link)),
            )
        ).all()
    }
    clean_public = frozenset(clean_ids.values())
    _hidden_org_rows, hidden_orgs = _hidden_organization_ids(db)
    hidden_org_ids = set(_hidden_org_rows)

    by_site: dict[uuid.UUID, list[Any]] = defaultdict(list)
    plants: dict[str, frozenset[str]] = {}
    links: dict[str, frozenset[str]] = {}
    for row in db.execute(
        select(
            SiteMember.site_id,
            SiteMember.basis,
            SiteMember.grouping_evidence,
            Proposal.id,
            Proposal.public_id,
            Proposal.slug,
            Proposal.interconnection_point_id,
            Proposal.sponsor_org_id,
        ).join(Proposal, Proposal.id == SiteMember.proposal_id)
    ).all():
        by_site[row.site_id].append(row)
        plants[row.public_id] = frozenset(str(p) for p in (row.basis or {}).get("plant_ids") or ())
        links[row.public_id] = _stored_partners(row.grouping_evidence)

    # Points at members: whether their register is gated, and whether any proposal at them is not
    # public (a neighbour the detail could list).
    point_ids = sorted(
        {r.interconnection_point_id for rs in by_site.values() for r in rs if r.interconnection_point_id},
        key=str,
    )
    point_public: dict[uuid.UUID, str] = {}
    gated_points: set[uuid.UUID] = set()
    risky_points: set[uuid.UUID] = set()
    for chunk in _chunks(point_ids):
        for pid, pub, source_id, licence_id in db.execute(
            select(
                InterconnectionPoint.id,
                InterconnectionPoint.public_id,
                InterconnectionPoint.source_id,
                InterconnectionPoint.licence_id,
            ).where(InterconnectionPoint.id.in_(chunk))
        ).all():
            point_public[pid] = pub
            if source_id in gated_src or licence_id in gated_lic:
                gated_points.add(pid)
        for point_id, prop_id in db.execute(
            select(Proposal.interconnection_point_id, Proposal.id).where(
                Proposal.interconnection_point_id.in_(chunk)
            )
        ).all():
            if prop_id not in clean_ids:
                risky_points.add(point_id)
    risky_points |= gated_points
    clean_points = frozenset(pub for pid, pub in point_public.items() if pid not in gated_points)

    # Anchored assets and their owners: gated register or licence, or an owner that is not public.
    asset_ids: set[uuid.UUID] = set()
    for site in site_rows:
        for value in ((site.anchors or {}).get("assets") or {}).values():
            try:
                asset_ids.add(uuid.UUID(str(value)))
            except ValueError:
                continue
    asset_public: dict[uuid.UUID, str] = {}
    gated_assets: set[uuid.UUID] = set()
    risky_assets: set[uuid.UUID] = set()
    for chunk in _chunks(sorted(asset_ids, key=str)):
        for aid, pub, source_id, licence_id in db.execute(
            select(Asset.id, Asset.public_id, Asset.source_id, Asset.licence_id).where(Asset.id.in_(chunk))
        ).all():
            asset_public[aid] = pub
            if source_id in gated_src or licence_id in gated_lic:
                gated_assets.add(aid)
        for aid, org_id, source_id, licence_id in db.execute(
            select(
                AssetOwner.asset_id, AssetOwner.organization_id, AssetOwner.source_id, AssetOwner.licence_id
            ).where(AssetOwner.asset_id.in_(chunk))
        ).all():
            if org_id in hidden_org_ids or source_id in gated_src or licence_id in gated_lic:
                risky_assets.add(aid)
    risky_assets |= gated_assets
    clean_assets = frozenset(pub for aid, pub in asset_public.items() if aid not in gated_assets)

    breaches: list[Breach] = []
    shown = 0
    probe: list[str] = []
    hidden: list[tuple[str, str]] = []
    hidden_members: dict[str, list[tuple[str, str]]] = {}
    clean_members: dict[str, list[tuple[str, str]]] = {}
    expected: dict[str, int] = {}  # member public id -> its public group's size (0: no site)
    render: list[Site] = []
    enabled = sites_enabled()
    for site in sorted(site_rows, key=lambda s: s.public_id):
        rows = by_site.get(site.id, [])
        public_rows = [r for r in rows if r.id in clean_ids]
        servable = enabled and site.retired_at is None and site.review_flag is None
        roots = restated_groups([r.public_id for r in public_rows], plants, links)
        sizes = Counter(roots.values())
        for r in public_rows:
            size = sizes[roots[r.public_id]] if servable else 0
            expected[r.public_id] = size if size >= 2 else 0
        if not servable:
            if site.retired_at is None:
                hidden.append((site.public_id, site.review_flag or "kill_switch"))
            if sites_api.served_groups(db, site, "public", with_sources=False):
                why = "retired" if site.retired_at is not None else (site.review_flag or "kill_switch")
                breaches.append(Breach(SITES, site.public_id, None, f"site_hidden_printed:{why}"))
            continue
        if any(n >= 2 for n in sizes.values()):
            shown += 1
        assets_here = {
            uuid.UUID(str(v)) for v in ((site.anchors or {}).get("assets") or {}).values() if _is_uuid(v)
        }
        risky = (
            len(public_rows) < len(rows)
            or any(r.sponsor_org_id in hidden_org_ids for r in rows)
            or any(r.interconnection_point_id in risky_points for r in rows)
            or bool(assets_here & risky_assets)
        )
        if not risky:
            continue
        render.append(site)
        hidden_members[site.public_id] = [(r.public_id, r.slug) for r in rows if r.id not in clean_ids]
        clean_members[site.public_id] = [(r.public_id, r.slug) for r in public_rows]
        if hidden_members[site.public_id]:
            probe.append(site.public_id)

    facts = _SiteFacts(
        clean=clean_public,
        plants=plants,
        links=links,
        hidden_members=hidden_members,
        clean_members=clean_members,
        probe=probe,
        hidden=hidden,
        expected=expected,
        clean_assets=clean_assets,
        clean_points=clean_points,
        hidden_orgs=hidden_orgs,
    )
    # The detail, through the builders `GET /v1/sites/{id}` calls: every group a public caller could
    # be served (the largest by default, any other through `?member=`).
    for site in render:
        for group in sites_api.served_groups(db, site, "public"):
            data, _rows = sites_api.serialize_site(db, group, "public")
            for reason in site_page_offences(data, facts):
                breaches.append(Breach(SITES, site.public_id, None, reason))
    # The embeds, as the proposal detail and the bulk stream render them, for every public member of
    # a rendered site or of a site that must serve nothing (flagged; switched off).
    wanted = {pub for s in render for pub, _slug in clean_members.get(s.public_id, [])}
    for site in site_rows:
        if site.retired_at is None and (site.review_flag is not None or not enabled):
            wanted.update(r.public_id for r in by_site.get(site.id, []))
    embed_ids = sorted((cid for cid, pub in clean_ids.items() if pub in wanted), key=str)
    for chunk in _chunks(embed_ids):
        proposals = list(
            db.scalars(select(Proposal).where(Proposal.id.in_(chunk)).order_by(Proposal.public_id)).all()
        )
        for surface, link_ok in (("proposal_embed", None), ("bulk_embed", _api_redistributable)):
            embeds = sites_api.proposal_site_embeds(db, proposals, "public", link_ok=link_ok)
            for proposal in proposals:
                offence = site_embed_offence(
                    embeds.get(proposal.id), expected.get(proposal.public_id, 0), facts
                )
                if offence:
                    breaches.append(
                        Breach(SITES, proposal.public_id, None, f"site_embed_printed:{surface}:{offence}")
                    )
    return shown, breaches, facts


def _api_redistributable(source: Source) -> bool:
    """The bulk stream's link rule (`services/api/bulk.py::_api_redistributable`), restated."""
    return bool(source.licence.allows_api_redistribution)


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def _hidden_candidates(db: Session, gated_src: Mapping[str, str], *, limit: int) -> list[_Candidate]:
    """Rows that must be hidden whatever the predicate says, for the served pass: taken-down and
    pending records, records whose *only* active evidence is gated (a mixed-provenance record is
    rightly public on its clean evidence, docs/21 §8, so it is not a candidate here; its gated
    link is the store pass's `source_links` check), assets on gated sources."""
    out: list[_Candidate] = []
    for surface, model in (("proposals", Proposal), ("opportunities", Opportunity)):
        rows = db.execute(
            select(model.public_id, model.publish_state)
            .where(model.publish_state.in_(HIDDEN_RECORD_STATES))
            .order_by(model.public_id)
            .limit(limit)
        ).all()
        out.extend(_Candidate(surface, pid, f"publish_state={state}") for pid, state in rows)
    if gated_src:
        gated_ids = list(gated_src)
        clean_p = aliased(ProposalSource)
        clean_o = aliased(OpportunitySource)
        for surface, model, link_model, fk, clean, clean_fk in (
            ("proposals", Proposal, ProposalSource, ProposalSource.proposal_id, clean_p, clean_p.proposal_id),
            (
                "opportunities",
                Opportunity,
                OpportunitySource,
                OpportunitySource.opportunity_id,
                clean_o,
                clean_o.opportunity_id,
            ),
        ):
            has_clean = exists(
                select(clean.id).where(
                    clean_fk == model.id,
                    clean.active.is_(True),
                    clean.source_id.not_in(gated_ids),
                )
            )
            rows = db.execute(
                select(model.public_id, func.min(link_model.source_id))
                .join(link_model, fk == model.id)
                .where(link_model.active.is_(True), link_model.source_id.in_(gated_ids), ~has_clean)
                .group_by(model.public_id)
                .order_by(model.public_id)
                .limit(limit)
            ).all()
            out.extend(_Candidate(surface, pid, f"only_gated_source={sid}") for pid, sid in rows)
        rows = db.execute(
            select(Asset.public_id, Asset.source_id)
            .where(Asset.source_id.in_(gated_ids))
            .order_by(Asset.public_id)
            .limit(limit)
        ).all()
        out.extend(_Candidate("assets", pid, f"source={sid}") for pid, sid in rows)
    return out


def audit_store(
    db: Session,
    *,
    posture: str,
    now: dt.datetime | None = None,
    register_path: Path | None = None,
) -> dict[str, Any]:
    """The store pass (module docstring, 1). Returns the result dict minus the served pass."""
    now = now or dt.datetime.now(dt.UTC)
    publishable = publishable_reuse_classes(posture)
    register = register_gated_sources(posture, register_path)
    gated_src = gated_sources(db, posture, register)
    gated_lic = gated_licences(db, posture)

    counts: dict[str, dict[str, int]] = {s: {"shown": 0, "breaches": 0} for s in SURFACES}
    breaches: list[Breach] = []
    for surface, model, link_model, fk in (
        ("proposals", Proposal, ProposalSource, ProposalSource.proposal_id),
        ("opportunities", Opportunity, OpportunitySource, OpportunitySource.opportunity_id),
    ):
        shown, found = _audit_records(
            db,
            surface=surface,
            model=model,
            link_model=link_model,
            fk=fk,
            gated_src=gated_src,
            publishable=publishable,
            now=now,
        )
        counts[surface]["shown"] = shown
        breaches.extend(found)
    found, field_facts_list = _audit_record_fields(db, gated_src=gated_src, now=now)
    breaches.extend(found)
    breaches.extend(_audit_derived_only_raw(db, now=now))
    shown, found = _audit_events(db, gated_src=gated_src, gated_lic=gated_lic, now=now)
    counts["events"]["shown"] = shown
    breaches.extend(found)
    shown, found = _audit_organizations(db, gated_src=gated_src, now=now)
    counts["organizations"]["shown"] = shown
    breaches.extend(found)
    shown, found = _audit_assets(db, gated_src=gated_src, gated_lic=gated_lic, now=now)
    counts["assets"]["shown"] = shown
    breaches.extend(found)
    shown, found, point_facts = _audit_interconnection_points(
        db, gated_src=gated_src, gated_lic=gated_lic, publishable=publishable, now=now
    )
    counts[POINTS]["shown"] = shown
    breaches.extend(found)
    shown, found, site_facts = _audit_sites(
        db, gated_src=gated_src, gated_lic=gated_lic, publishable=publishable, now=now
    )
    counts[SITES]["shown"] = shown
    breaches.extend(found)
    withheld = withheld_names(db)
    hidden = _hidden_organization_ids(db) if not withheld.empty else ([], frozenset[str]())
    found, name_probes = _audit_asset_operator_names(db, now=now, withheld=withheld, hidden=hidden)
    breaches.extend(found)
    # The links surface has no independent row count of its own: what it "shows" is the active
    # link rows of the shown proposals, opportunities and assets.
    counts["source_links"]["shown"] = _shown_link_count(db, now)
    for breach in breaches:
        counts[breach.surface]["breaches"] += 1

    return {
        "run_id": str(uuid.uuid4()),
        "run_at": now.isoformat(),
        "posture": posture,
        "posture_source": "platform" if posture == platform_posture() else "override",
        "publishable_reuse_classes": list(publishable),
        "register_path": str(register_path or SOURCES_YAML),
        "register_gated_sources": len(register),
        "gated_sources": dict(sorted(gated_src.items())),
        "counts": counts,
        "breaches": [asdict(b) for b in breaches[:BREACH_CAP]],
        "breach_total": len(breaches),
        "breach_cap": BREACH_CAP,
        "breaches_truncated": len(breaches) > BREACH_CAP,
        "served": {"checked": 0, "leaks": 0, "inconclusive": 0, "web_pages": "not_needed", "checks": []},
        # Stored links to gated sources on shown records that every surface withholds: the
        # routine state after a source is unpublished, reported apart from breaches (QA-10).
        "withheld_links": _withheld_link_count(db, gated_src=gated_src, now=now),
        "field_checked_records": len(field_facts_list),
        "m11": len(breaches),
        "_breaches": breaches,  # in-memory only; stripped before persisting/returning
        "_withheld": withheld,  # in-memory only: the served pass scans with the same names
        "_hidden_org_ids": hidden[1],
        "_name_probes": name_probes,  # in-memory only
        "_points": point_facts,  # in-memory only: expected totals are never persisted
        "_sites": site_facts,  # in-memory only: member lists and slugs for the served pass
        "_field_facts": {f.public_id: f for f in field_facts_list},  # in-memory only: stored values
    }


def _shown_link_count(db: Session, now: dt.datetime) -> int:
    total = 0
    for model, link_model, fk, surface in (
        (Proposal, ProposalSource, ProposalSource.proposal_id, "proposals"),
        (Opportunity, OpportunitySource, OpportunitySource.opportunity_id, "opportunities"),
    ):
        total += int(
            db.scalar(
                select(func.count())
                .select_from(link_model)
                .join(model, fk == model.id)
                .where(link_model.active.is_(True), *PREDICATES[surface]("public", now))
            )
            or 0
        )
    total += int(
        db.scalar(
            select(func.count())
            .select_from(AssetSource)
            .join(Asset, Asset.id == AssetSource.asset_id)
            .where(*PREDICATES["assets"]("public", now))
        )
        or 0
    )
    return total


# ================================================================================ served pass
def _detail_path(surface: str, pid: str) -> str | None:
    if surface == "proposals":
        return f"/v1/proposals/{pid}"
    if surface == "opportunities":
        return f"/v1/opportunities/{pid}"
    if surface == "events":
        return f"/v1/events/{pid}"
    if surface == "organizations":
        return f"/v1/organizations/{pid}"
    if surface == "assets":
        return f"/v1/assets/{pid}"
    if surface == "source_links":
        if pid.startswith("prop_"):
            return f"/v1/proposals/{pid}/sources"
        if pid.startswith("opp_"):
            return f"/v1/opportunities/{pid}/sources"
        if pid.startswith("asset_"):
            return f"/v1/assets/{pid}"
    if surface == POINTS:
        # A point breach is about the point's own page; an embed breach about the proposal's.
        if pid.startswith("poi_"):
            return f"/v1/interconnection-points/{pid}"
        if pid.startswith("prop_"):
            return f"/v1/proposals/{pid}"
    if surface == SITES:
        # The same split: a site's own page, or a proposal's `site` embed.
        if pid.startswith("site_"):
            return f"/v1/sites/{pid}"
        if pid.startswith("prop_"):
            return f"/v1/proposals/{pid}"
    return None


def _mentions_source(node: Any, source_id: str) -> bool:
    if isinstance(node, dict):
        if node.get("source_id") == source_id:
            return True
        return any(_mentions_source(v, source_id) for v in node.values())
    if isinstance(node, list):
        return any(_mentions_source(v, source_id) for v in node)
    return False


@contextmanager
def anonymous_client(session_factory: sessionmaker[Session]) -> Iterator[Any]:
    """The real app, no credentials, reading the audited store. `get_db` is overridden for the
    duration and the previous override (a test's, if any) is put back."""
    from fastapi.testclient import TestClient

    from services.api.app import app
    from services.api.deps import get_db

    def _override() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _override
    # The site's own service identity (`web/api_client.py` does the same): the public tier, never
    # metered against the anonymous per-address budget, so the served pass cannot starve itself
    # into 429s (QA-10). The app reads the variable per request, in this process.
    token = os.environ.setdefault("API_INTERNAL_TOKEN", secrets.token_urlsafe(32))
    try:
        with TestClient(app, headers={"X-Internal-Token": token}) as client:
            yield client
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


def served_pass(
    session_factory: sessionmaker[Session],
    result: dict[str, Any],
    candidates: list[_Candidate],
    *,
    sample: int = SERVED_SAMPLE,
) -> None:
    """The served pass (module docstring, 2). Mutates `result` in place: annotates the sampled
    breaches, appends `served_hidden` breaches, fills `result["served"]` and recomputes `m11`."""
    breaches: list[Breach] = result["_breaches"]
    already = {(b.surface, b.public_id) for b in breaches}
    checks: list[dict[str, Any]] = []
    leaks = 0
    with anonymous_client(session_factory) as client:
        for breach in breaches[:sample]:
            path = _detail_path(breach.surface, breach.public_id)
            if path is None:
                continue
            response = client.get(path)
            breach.served_status = response.status_code
            leak = response.status_code == 200
            if leak and breach.surface == "source_links" and breach.source_id:
                leak = _mentions_source(response.json(), breach.source_id)
            if leak and breach.reason.startswith("withheld_name_printed:"):
                # The asset is rightly served; the leak is the name in it.
                leak = bool(_served_name_paths(response.json().get("data") or {}, result, breach.public_id))
            if leak and breach.surface == POINTS:
                leak = _point_breach_served(breach, response.json().get("data") or {}, result)
            if leak and breach.surface == SITES:
                leak = _site_breach_served(breach, response.json().get("data") or {}, result)
            if leak and breach.reason.startswith("field_from_gated_source:"):
                # The record is rightly served; the leak is the field in it.
                facts = result["_field_facts"][breach.public_id]
                field = breach.reason.split(":", 1)[1]
                served = field_offences(facts, response.json().get("data") or {})
                leak = any(name == field for name, _ in served)
            breach.served_leak = leak
            leaks += int(leak)
            checks.append(
                {
                    "kind": "breach",
                    "surface": breach.surface,
                    "public_id": breach.public_id,
                    "path": path,
                    "status": response.status_code,
                    "leak": leak,
                }
            )
        # A candidate the store pass already counted is not requested again: its breach is in
        # the list once, and the sampled-breach loop above is where its served status lands.
        fresh = [c for c in candidates if (c.surface, c.public_id) not in already]
        for candidate in fresh[:sample]:
            path = _detail_path(candidate.surface, candidate.public_id)
            if path is None:
                continue
            response = client.get(path)
            leak = response.status_code == 200
            leaks += int(leak)
            checks.append(
                {
                    "kind": "must_be_hidden",
                    "surface": candidate.surface,
                    "public_id": candidate.public_id,
                    "path": path,
                    "status": response.status_code,
                    "leak": leak,
                    "why": candidate.why,
                }
            )
            if leak:
                breach = Breach(
                    candidate.surface, candidate.public_id, None, f"served_hidden:{candidate.why}", 200, True
                )
                breaches.append(breach)
                result["counts"][candidate.surface]["breaches"] += 1
        leaks += _served_field_checks(client, result, already, checks, sample=sample)
        leaks += _served_name_checks(client, result, already, checks, sample=sample)
        leaks += _served_point_checks(client, result, already, checks, sample=sample)
        leaks += _served_site_checks(client, result, already, checks, sample=sample)
    # A status that is neither "served" (200) nor "hidden" (404) — a 429 from the public tier's
    # hourly budget, a 5xx — proves nothing either way; it is counted so a run whose served pass
    # was starved cannot read as a clean one.
    inconclusive = sum(1 for c in checks if c["status"] not in (200, 404))
    result["served"] = {
        "checked": len(checks),
        "leaks": leaks,
        "inconclusive": inconclusive,
        "web_pages": result.pop("_web_pages", "not_needed"),
        "checks": checks,
    }
    result["breaches"] = [asdict(b) for b in breaches[:BREACH_CAP]]
    result["breach_total"] = len(breaches)
    result["breaches_truncated"] = len(breaches) > BREACH_CAP
    result["m11"] = len(breaches)


def _served_field_checks(
    client: Any,
    result: dict[str, Any],
    already: set[tuple[str, str]],
    checks: list[dict[str, Any]],
    *,
    sample: int,
) -> int:
    """The served half of the field-level check: the detail of a sample of mixed-provenance
    records the store pass found clean, through the real app, must not print a gated source's
    value, count a gated link or place the record at a gated source's point (`field_offences`).
    This is what catches a serving path that bypasses `services/api/serialize.py`. Each offence is
    a breach `served_field_leak:<field>`. A status other than 200/404 is inconclusive."""
    facts_by_id: Mapping[str, _FieldFacts] = result.get("_field_facts", {})
    breaches: list[Breach] = result["_breaches"]
    leaks = 0
    probes = [f for pid, f in sorted(facts_by_id.items()) if (f.surface, pid) not in already][:sample]
    for facts in probes:
        path = _detail_path(facts.surface, facts.public_id)
        if path is None:  # pragma: no cover - both record surfaces have a detail path
            continue
        response = client.get(path)
        data = response.json().get("data") or {} if response.status_code == 200 else {}
        offences = field_offences(facts, data) if response.status_code == 200 else []
        checks.append(
            {
                "kind": "field_provenance",
                "surface": facts.surface,
                "public_id": facts.public_id,
                "path": path,
                "status": response.status_code,
                "leak": bool(offences),
            }
        )
        for name, source_id in offences:
            leaks += 1
            breaches.append(
                Breach(facts.surface, facts.public_id, source_id, f"served_field_leak:{name}", 200, True)
            )
            result["counts"][facts.surface]["breaches"] += 1
    return leaks


def _served_name_paths(data: Mapping[str, Any], result: Mapping[str, Any], asset_public_id: str) -> list[str]:
    withheld: WithheldNames | None = result.get("_withheld")
    if withheld is None or withheld.empty:
        return []
    edge = any(p.operator_edge for p in result.get("_name_probes", ()) if p.target == asset_public_id)
    return withheld_name_paths(
        data, withheld, operator_edge=edge, hidden_org_ids=result.get("_hidden_org_ids", frozenset())
    )


def _served_name_checks(
    client: Any,
    result: dict[str, Any],
    already: set[tuple[str, str]],
    checks: list[dict[str, Any]],
    *,
    sample: int,
) -> int:
    """The served half of the withheld-name check (module docstring): a sample of linked assets'
    detail pages must not print a withheld name, and an asset search for a withheld spelling must
    neither print one nor return an asset it could only have matched through that name. Returns the
    number of leaks; each is also a breach (`withheld_name_served`, `withheld_name_searchable`)."""
    probes: list[_NameProbe] = result.get("_name_probes", [])
    breaches: list[Breach] = result["_breaches"]
    leaks = 0
    details = [p for p in probes if p.kind == "detail" and ("assets", p.target) not in already]
    searches = [p for p in probes if p.kind == "search"]
    for probe in [*details[:sample], *searches[:sample]]:
        if probe.kind == "detail":
            path = f"/v1/assets/{probe.target}"
            response = client.get(path)
            data = response.json().get("data") if response.status_code == 200 else None
            paths = _served_name_paths(data or {}, result, probe.target)
            offenders = [(probe.target, f"withheld_name_served:detail:{p}") for p in paths[:1]]
        else:
            path = "/v1/assets"
            response = client.get(path, params={"q": probe.target, "limit": 200})
            rows = (response.json().get("data") or []) if response.status_code == 200 else []
            offenders = []
            for row in rows:
                pid = str(row.get("public_id"))
                if pid in probe.assets:
                    offenders.append((pid, "withheld_name_searchable:operator_name"))
                row_paths = _served_name_paths(row, result, pid)
                if row_paths:
                    offenders.append((pid, f"withheld_name_served:list:{row_paths[0]}"))
        leak = bool(offenders)
        leaks += int(leak)
        # A search probe names the asset it caught, never the withheld spelling it searched for.
        checked_id = probe.target if probe.kind == "detail" else (offenders[0][0] if offenders else "search")
        checks.append(
            {
                "kind": "withheld_name",
                "surface": "assets",
                "public_id": checked_id,
                "path": path if probe.kind == "detail" else f"{path}?q=<withheld spelling>",
                "status": response.status_code,
                "leak": leak,
                "why": f"{probe.kind}:withheld organisation name",
            }
        )
        for pid, reason in offenders:
            breaches.append(Breach("assets", pid, None, reason, response.status_code, True))
            result["counts"]["assets"]["breaches"] += 1
    return leaks


def _point_detail_offence(data: Mapping[str, Any], facts: _PointFacts) -> str | None:
    """Why a served point detail (or a list row, which carries the same `totals`) breaches, or
    `None`: a point the store pass says is hidden, totals that are not the sum over clean
    proposals, a listed proposal or a change event about a proposal that is not clean at it."""
    public = str(data.get("public_id"))
    want = facts.expected.get(public)
    if want is None:
        return "point_hidden_served:point_not_visible"
    field_name = first_total_mismatch(data.get("totals") or {}, want, POINT_TOTAL_FIELDS)
    if field_name:
        return f"point_total_served:detail:{field_name}"
    clean = facts.clean_proposals.get(public, frozenset())
    if any(row.get("public_id") not in clean for row in data.get("proposals") or []):
        return "point_proposal_served:detail"
    for change in data.get("recent_changes") or []:
        if (change.get("subject") or {}).get("public_id") not in clean:
            return "point_change_served:subject_not_visible"
        if (change.get("provenance") or {}).get("source_id") in facts.gated_sources:
            return "point_change_served:event_source_gated"
        public_at = change.get("public_at")
        if public_at is None or (
            facts.now is not None and _aware(dt.datetime.fromisoformat(str(public_at))) > facts.now
        ):
            return "point_change_served:event_not_yet_public"
    return None


def _embed_offence(embed: Mapping[str, Any] | None, facts: _PointFacts) -> str | None:
    """Why a served proposal detail's `interconnection_point` breaches, or `None`."""
    if not embed:
        return None
    want = facts.expected.get(str(embed.get("public_id")))
    if want is None:
        return "point_embed_served:proposal_embed:point_not_visible"
    field_name = first_total_mismatch(embed, want, EMBED_TOTAL_FIELDS)
    return f"point_total_served:proposal_embed:{field_name}" if field_name else None


def _point_breach_served(breach: Breach, data: Mapping[str, Any], result: Mapping[str, Any]) -> bool:
    """Whether a sampled store-pass point breach is on the served page too. The page itself is
    rightly served for a clean point or a clean proposal; the leak is what it carries."""
    facts: _PointFacts | None = result.get("_points")
    if facts is None:
        return True
    if breach.public_id.startswith("prop_"):
        # A bulk-only breach (the licence's redistribution flag) cannot be confirmed anonymously:
        # the proposal detail rightly carries that embed.
        return _embed_offence(data.get("interconnection_point"), facts) is not None
    if breach.reason.startswith(("point_shown_printed:", "point_listed_printed:")):
        return True
    return _point_detail_offence(data, facts) is not None


def _oracle_view(response: Any) -> tuple[int, Any, Any]:
    """What the `interconnection_point_id=` filter disclosed: status, rows and paging (the `meta`
    block carries a request id and timestamps, which differ on every request)."""
    try:
        body = response.json()
    except ValueError:
        return response.status_code, None, None
    return response.status_code, body.get("data"), body.get("page")


#: The active-queue figure on a point's web page (`web/templates/interconnection_point_detail.html`).
_WEB_ACTIVE_MW = re.compile(r"<dt>Active queued \(MW\)</dt><dd[^>]*>\s*([^<\s]+)")


def web_active_mw_label(html: str) -> str | None:
    """The active queued MW a point's web page prints, as printed, or `None` when the page carries
    no such field -- which the served pass counts as a disagreement, so a template change that
    moves the figure fails the clean-store test rather than silently passing every run."""
    match = _WEB_ACTIVE_MW.search(html)
    return match.group(1) if match else None


def web_active_mw_matches(label: str | None, expected_mw: float) -> bool:
    """Whether a printed active-MW label states the expected total. Compared as a number to the
    page's one decimal (`web/formatting.py::mw` prints `2,000` and `1,300.5`), so the check is
    about the total a reader is served, not its spelling; a missing or unreadable label is a
    disagreement."""
    if label is None:
        return False
    try:
        printed = float(label.replace(",", ""))
    except ValueError:
        return False
    return abs(printed - round(float(expected_mw), 1)) < 0.05


@contextmanager
def site_client() -> Iterator[Any | None]:
    """The public site, reading the audited store through the in-process API with the site's own
    service identity (`web/api_client.py::build_client`; no credential, so the public tier), or
    `None` where the `web` package is not installed -- the scheduler image ships only `services`
    (`infra/docker/Dockerfile`). Deferred imports: `services` never imports `web` at module load.
    Call inside `anonymous_client`, whose `get_db` override the site's API calls then share."""
    try:
        from web.api_client import build_client
        from web.app import app as web_app
    except ImportError:  # pragma: no cover - the scheduler image; the test suite always has `web`
        yield None
        return
    from fastapi.testclient import TestClient

    previous = web_app.state.__dict__.get("api_client")
    web_app.state.api_client = build_client(api_base_url="")
    try:
        with TestClient(web_app) as client:
            yield client
    finally:
        if previous is None:
            web_app.state.__dict__.pop("api_client", None)
        else:
            web_app.state.api_client = previous


def _served_point_checks(
    client: Any,
    result: dict[str, Any],
    already: set[tuple[str, str]],
    checks: list[dict[str, Any]],
    *,
    sample: int,
) -> int:
    """The served half of the point checks (module docstring). Returns the number of leaks; each
    offending response is also a breach. Spends no request when the store holds no point."""
    facts: _PointFacts | None = result.get("_points")
    size = min(sample, POINT_SERVED_SAMPLE)
    if facts is None or not facts.any_points or size <= 0:
        return 0
    breaches: list[Breach] = result["_breaches"]
    leaks = 0

    def record(kind: str, pub: str, path: str, status: int, reason: str | None, why: str) -> None:
        nonlocal leaks
        if status not in (200, 404):
            # A 429 or a 5xx proves nothing either way (QA-10: a throttled oracle request was
            # scored a leak): inconclusive, counted by `served_pass`, never a breach.
            reason = None
        checks.append(
            {
                "kind": kind,
                "surface": POINTS,
                "public_id": pub,
                "path": path,
                "status": status,
                "leak": reason is not None,
                "why": why,
            }
        )
        if reason is not None:
            leaks += 1
            breaches.append(Breach(POINTS, pub, None, reason, status, True))
            result["counts"][POINTS]["breaches"] += 1

    # The list's first page (its default order puts the largest queues first).
    response = client.get("/v1/interconnection-points", params={"limit": 200})
    rows = (response.json().get("data") or []) if response.status_code == 200 else []
    offenders = [
        (str(r.get("public_id")), reason) for r in rows if (reason := _point_detail_offence(r, facts))
    ]
    listed: list[tuple[str, str | None]] = list(offenders) or [("list", None)]
    for pub, reason in listed:
        listed_reason = reason.replace(":detail:", ":list:") if reason else None
        if listed_reason and listed_reason.startswith("point_hidden_served:"):
            listed_reason = "point_listed_served:point_not_visible"
        record(
            "point_totals",
            pub,
            "/v1/interconnection-points",
            response.status_code,
            listed_reason,
            "list",
        )

    with site_client() as site:
        result["_web_pages"] = "checked" if site is not None else "unavailable"
        hidden = [(pub, why) for pub, why in facts.hidden if (POINTS, pub) not in already][:size]
        if hidden:
            baseline = _oracle_view(
                client.get("/v1/proposals", params={"interconnection_point_id": UNKNOWN_POINT_ID})
            )
        for pub, why in hidden:
            path = f"/v1/interconnection-points/{pub}"
            response = client.get(path)
            reason = f"point_hidden_served:{why}" if response.status_code == 200 else None
            record("must_be_hidden", pub, path, response.status_code, reason, why)
            response = client.get("/v1/proposals", params={"interconnection_point_id": pub})
            reason = None if _oracle_view(response) == baseline else "point_oracle_served:proposals_filter"
            record(
                "point_oracle",
                pub,
                f"/v1/proposals?interconnection_point_id={pub}",
                response.status_code,
                reason,
                why,
            )
            if site is not None:
                page = site.get(f"/interconnection-points/{pub}")
                # A 503 from the site proves nothing either way; it is counted inconclusive.
                reason = f"point_hidden_served:web_detail:{why}" if page.status_code == 200 else None
                record(
                    "must_be_hidden",
                    pub,
                    f"/interconnection-points/{pub}",
                    page.status_code,
                    reason,
                    why,
                )

        busiest = sorted(facts.expected, key=lambda pub: (-float(facts.expected[pub]["active_mw"]), pub))
        for pub in [pub for pub in busiest if (POINTS, pub) not in already][:size]:
            path = f"/v1/interconnection-points/{pub}"
            response = client.get(path)
            data = response.json().get("data") if response.status_code == 200 else None
            record(
                "point_totals",
                pub,
                path,
                response.status_code,
                _point_detail_offence(data or {}, facts) if data else None,
                "detail",
            )
            proposal = min(facts.clean_proposals[pub])
            response = client.get(f"/v1/proposals/{proposal}")
            embed = (
                (response.json().get("data") or {}).get("interconnection_point")
                if response.status_code == 200
                else None
            )
            record(
                "point_totals",
                proposal,
                f"/v1/proposals/{proposal}",
                response.status_code,
                _embed_offence(embed, facts),
                "proposal_embed",
            )
            if site is not None:
                page = site.get(f"/interconnection-points/{pub}")
                expected_mw = float(facts.expected[pub]["active_mw"])
                reason = (
                    "point_total_served:web_detail:active_mw"
                    if page.status_code == 200
                    and not web_active_mw_matches(web_active_mw_label(page.text), expected_mw)
                    else None
                )
                record(
                    "point_totals",
                    pub,
                    f"/interconnection-points/{pub}",
                    page.status_code,
                    reason,
                    "web_detail",
                )
    return leaks


def _site_breach_served(breach: Breach, data: Mapping[str, Any], result: Mapping[str, Any]) -> bool:
    """Whether a sampled store-pass site breach is on the served page too: the site page (its
    largest group) or the proposal's `site` embed still prints what the store pass found."""
    facts: _SiteFacts | None = result.get("_sites")
    if facts is None:
        return True
    if breach.public_id.startswith("prop_"):
        # A bulk-only offence cannot be confirmed anonymously: the detail's embed is the probe.
        return site_embed_offence(data.get("site"), _allowed(facts, breach.public_id), facts) is not None
    if breach.reason.startswith("site_hidden_printed:"):
        return True
    return bool(site_page_offences(data, facts))


def _allowed(facts: _SiteFacts, member: str) -> int:
    """The size of `member`'s group over public members of a servable site, restated (0: no site)."""
    return facts.expected.get(member, 0)


def _hidden_in_page(html: str, hidden: list[tuple[str, str]]) -> bool:
    """Whether a page names a member no public page may print: its public id, or a link to its
    record page."""
    return any(pub in html or f'/proposals/{slug}"' in html for pub, slug in hidden)


def _served_site_checks(
    client: Any,
    result: dict[str, Any],
    already: set[tuple[str, str]],
    checks: list[dict[str, Any]],
    *,
    sample: int,
) -> int:
    """The served half of the site checks: for a sample of servable sites that hold a member that is
    not public, the API detail (`site_page_offences`), one public member's `site` embed, the site's
    web page and that member's web page (neither may name a hidden member); for a sample of flagged
    sites, the API detail and the web page must answer the unknown id's 404. Returns the number of
    leaks; each is also a breach. Spends no request when the store holds no site."""
    facts: _SiteFacts | None = result.get("_sites")
    size = min(sample, SITE_SERVED_SAMPLE)
    if facts is None or size <= 0:
        return 0
    breaches: list[Breach] = result["_breaches"]
    leaks = 0

    def record(kind: str, pub: str, path: str, status: int, reason: str | None, why: str) -> None:
        nonlocal leaks
        if status not in (200, 404):
            reason = None  # inconclusive, counted by `served_pass`, never a breach
        checks.append(
            {
                "kind": kind,
                "surface": SITES,
                "public_id": pub,
                "path": path,
                "status": status,
                "leak": reason is not None,
                "why": why,
            }
        )
        if reason is not None:
            leaks += 1
            breaches.append(Breach(SITES, pub, None, reason, status, True))
            result["counts"][SITES]["breaches"] += 1

    probes = [pub for pub in facts.probe if (SITES, pub) not in already][:size]
    flagged = [(pub, why) for pub, why in facts.hidden if (SITES, pub) not in already][:size]
    if not probes and not flagged:
        return 0
    with site_client() as site:
        if site is not None or "_web_pages" not in result:
            result["_web_pages"] = "checked" if site is not None else "unavailable"
        for pub in probes:
            path = f"/v1/sites/{pub}"
            response = client.get(path)
            data = (response.json().get("data") or {}) if response.status_code == 200 else {}
            offences = site_page_offences(data, facts) if data else []
            reason = offences[0].replace("_printed", "_served", 1) if offences else None
            record("site_members", pub, path, response.status_code, reason, "detail")
            members = facts.clean_members.get(pub) or []
            if not members:
                continue
            member, slug = members[0]
            path = f"/v1/proposals/{member}"
            response = client.get(path)
            embed = (response.json().get("data") or {}).get("site") if response.status_code == 200 else None
            offence = site_embed_offence(embed, _allowed(facts, member), facts)
            record(
                "site_members",
                member,
                path,
                response.status_code,
                f"site_embed_served:proposal_embed:{offence}" if offence else None,
                "proposal_embed",
            )
            if site is None:
                continue
            hidden = facts.hidden_members.get(pub) or []
            page = site.get(f"/sites/{pub}")
            reason = (
                "site_member_served:web_detail"
                if page.status_code == 200 and _hidden_in_page(page.text, hidden)
                else None
            )
            record("site_members", pub, f"/sites/{pub}", page.status_code, reason, "web_detail")
            page = site.get(f"/proposals/{slug}")
            reason = (
                "site_member_served:web_panel"
                if page.status_code == 200 and _hidden_in_page(page.text, hidden)
                else None
            )
            record("site_members", member, f"/proposals/{slug}", page.status_code, reason, "web_panel")
        for pub, why in flagged:
            path = f"/v1/sites/{pub}"
            response = client.get(path)
            reason = f"site_hidden_served:{why}" if response.status_code == 200 else None
            record("must_be_hidden", pub, path, response.status_code, reason, why)
            if site is not None:
                page = site.get(f"/sites/{pub}")
                reason = f"site_hidden_served:web_detail:{why}" if page.status_code == 200 else None
                record("must_be_hidden", pub, f"/sites/{pub}", page.status_code, reason, why)
    return leaks


# ================================================================================ persistence
def persist_result(db: Session, result: Mapping[str, Any]) -> Event:
    """One `event` row per run (module docstring). `after` holds the whole result; the row is
    system-actored, unpublished on every tier, and idempotent per `run_id`."""
    payload = {k: v for k, v in result.items() if not k.startswith("_")}
    run_at = dt.datetime.fromisoformat(str(payload["run_at"]))
    event = Event(
        subject_type=SUBJECT_TYPE,
        subject_id=SUBJECT_ID,
        event_type=EVENT_TYPE,
        observed_at=run_at,
        published_at=None,
        public_at=None,
        before=None,
        after=payload,
        changed_keys=sorted(payload),
        actor_type="system",
        reason=AUDIT_REASON,
        job_id=f"{EVENT_TYPE}:{payload['run_id']}",
        idempotency_key=f"audit:{SUBJECT_TYPE}:{SUBJECT_ID}:{EVENT_TYPE}:{payload['run_id']}",
    )
    db.add(event)
    db.flush()
    return event


def latest_results(db: Session, *, limit: int = 1) -> list[Event]:
    stmt = (
        select(Event)
        .where(Event.event_type == EVENT_TYPE, Event.subject_type == SUBJECT_TYPE)
        .order_by(Event.seq.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


# ================================================================================ entry points
def resolve_posture(value: str | None) -> str:
    """`auto` (or empty) is the platform's posture; anything else is normalised the way
    `PLATFORM_POSTURE` itself is (an unrecognised value fails closed to `commercial`)."""
    if value is None or value.strip().lower() in ("", "auto"):
        return platform_posture()
    return normalise_posture(value)


def run_audit(
    session_factory: sessionmaker[Session],
    *,
    posture: str | None = "auto",
    now: dt.datetime | None = None,
    persist: bool = True,
    served_sample: int = SERVED_SAMPLE,
    register_path: Path | None = None,
) -> dict[str, Any]:
    """Both passes, then persistence. Three sessions, not one: the store pass reads, the served
    pass runs request-scoped sessions of its own (a 404 rolls one back, and in the SQLite test
    target every session shares one connection), and the persist step writes."""
    effective = resolve_posture(posture)
    with session_scope(session_factory) as db:
        result = audit_store(db, posture=effective, now=now, register_path=register_path)
        candidates = _hidden_candidates(db, result["gated_sources"], limit=served_sample)
    served_pass(session_factory, result, candidates, sample=served_sample)
    if persist:
        with session_scope(session_factory) as db:
            event = persist_result(db, result)
            result["event_id"] = public_id("evt", event.id)
    for key in [k for k in result if k.startswith("_")]:
        result.pop(key)
    if result["m11"] > 0:
        logger.error(
            "M-11 breach: %s gated or restricted rows reachable on a non-admin surface (S1, docs/04 S-9)",
            result["m11"],
            extra={"m11": result["m11"], "run_id": result["run_id"]},
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m services.visibility_audit.run", description="Nightly M-11 visibility audit."
    )
    parser.add_argument(
        "--posture",
        default="auto",
        help=f"auto (PLATFORM_POSTURE, the default) or one of {', '.join(PLATFORM_POSTURES)}",
    )
    parser.add_argument("--no-persist", action="store_true", help="do not write the audit event")
    parser.add_argument("--sample", type=int, default=SERVED_SAMPLE, help="served-pass sample size")
    parser.add_argument(
        "--json", action="store_true", help="print the full result as JSON (default: summary)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(message)s")
    factory = get_sessionmaker(get_engine(os.environ.get("DATABASE_URL")))
    result = run_audit(factory, posture=args.posture, persist=not args.no_persist, served_sample=args.sample)
    if args.json:
        sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")
    else:
        sys.stdout.write(json.dumps(summarise(result), indent=2, default=str) + "\n")
    return 1 if result["m11"] > 0 else 0


def summarise(result: Mapping[str, Any]) -> dict[str, Any]:
    """The result without its row lists — what the job logs and the admin list returns."""
    return {
        key: value
        for key, value in result.items()
        if key not in ("breaches", "gated_sources", "served") and not key.startswith("_")
    } | {"served": {k: v for k, v in result["served"].items() if k != "checks"}}


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


if __name__ == "__main__":  # pragma: no cover - exercised through `main()` in tests
    sys.exit(main())
