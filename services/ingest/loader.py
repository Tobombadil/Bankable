"""Idempotent loader: connector output (normalised parquet + `pipeline/diff.py` events) -> store.

Reads exactly what `pipeline/connectors/runner.py` writes (`docs/20-architecture.md` §3.2-§3.3):
a normalised `DataFrame` in the shape of `pipeline.connectors.base.PROPOSAL_COLUMNS` /
`OPPORTUNITY_COLUMNS`, and an events `DataFrame` in the shape of `pipeline.diff.diff_snapshots`'s
output. It never fetches or parses anything itself.

Gate (CLAUDE.md; docs/21 §8; docs/04 DA-11): a source whose `data/sources.yaml` `reuse` is
`restricted` or `unknown` is refused before any row is written — this mirrors
`pipeline.connectors.registry.Registry.instantiate`'s own gate. Both must hold independently: the
connector already refuses to write publishable parquet for such a source (`runner.py` routes it to
`QuarantineStore`), and this loader refuses to read it back in regardless of where the file came
from, so a gated source can never reach the public store by either path alone failing open.

Simplifications this sprint (no upstream producer yet — recorded here, not silently dropped):
  - **No cross-source fusion.** `pipeline/resolve.py` (entity resolution across sources) is a
    later, data-scientist-owned stage. Each `(source_id, source_record_id)` becomes its own
    `proposal`/`opportunity` row 1:1 with its `proposal_source`/`opportunity_source` row; the
    `min_reuse_class` and `field_provenance` machinery is still exercised, just over a single
    source per record until resolve.py lands.
  - **Publish state.** Admin per-record publish/unpublish (US-905) is out of scope this sprint.
    Every record from a publishable source (its gate already cleared, by construction of the
    refusal above) is loaded as `publish_state = "public"`; the admin sprint gains the ability to
    move individual records to `pending_review` / `unpublished` without a loader change. The
    *source's own* `publish_state` is likewise set straight to `public` for a gate-cleared
    (`open`/`attribution`) registry entry (docs/21 §5.4's `source_permits`) rather than the earlier
    `api_only` default that needed a separate admin-style flip — see `_is_derived_only_override`'s
    neighbourhood below and `services/README.md`'s "Sprint 2 fixes" for the history.

Geocoding (`services/ingest/geocode.py`, this sprint's fix — previously `location.geom` was left
null for every row, `web/data_loading.py::backfill_locations` was a frontend-side stopgap for it):
a connector-parsed county name geocodes to its US Census Gazetteer centroid (`county_centroid`); a
county that doesn't resolve (misspelling, multi-county span, non-US) falls back to a state centroid
(`state_centroid`) when a state is present; otherwise the location is `unknown` — counted as
unplaced, never dropped (docs/04 D-8).

Exact-point promotion (Sprint 3, `services/README.md` "EIA exact-point promotion"): before falling
back to the county/state geocoder above, `_get_or_create_location` looks for a real coordinate pair
on the row's raw payload (EIA-860M's `Latitude`/`Longitude` today — `_extract_exact_point` is a
plain key lookup, so any other source whose raw payload already carries the same keys is promoted
the same way with no further loader change). A validated pair (numeric, inside world bounds, not
the `(0, 0)` placeholder) wins outright per docs/04 D-8's placement precedence and is stored at
`precision = "exact"`, `kind = "point"`, `geocoder = "source_provided"` — this used to be
`web/data_loading.py::backfill_eia_exact_points`'s job, a frontend-side correction applied after
the fact; it is now the loader's own job, at ingest time, for every source, not just EIA-860M's
prototype special case. `derived_only` sources (below) never get this promotion regardless of what
their raw payload carries (docs/04 D-9: an exact coordinate is a raw field, withheld exactly like
any other raw field for those sources) — they keep falling through to the county/state centroid.

Fixed since `services/resolve/README.md` first observed them (both without changing the public
functions below):
  - **Intra-run id reuse** (docs/22 §5/§7.1: the ISO-NE/NYISO signature). A source record whose
    natural key (`source_id`, `source_record_id`) already exists with different content from an
    *earlier* run is an update, handled exactly as before through the diff/event path — and a
    dataframe that repeats the *same* connector `record_id` for that key more than once in this
    call (e.g. several historical snapshots of one record bundled into one call) is likewise an
    ordinary sequential update, not reuse. Only a *different* `record_id` sharing that natural key
    inside the *same* call is true reuse, and those are never merged: every member of such a
    group is stored under `<source_record_id>#<content disambiguator>`
    (`pipeline.connectors.dedupe.content_disambiguator`, a hash of the row's stable fields —
    name, capacity, county/state, technology — the same key `Connector.finalize` puts on the
    `record_id`), a data-quality warning is recorded on `source_run.dq` and `LoadResult.warnings`.
    Until 2026-09-18 the suffix was positional (`#2`, `#3` in frame order), so a row reorder in
    the source swapped the two identities and fabricated a withdrawal (audit §3.1, live on
    NYISO). **Legacy keys on read**: rows already stored as `X` / `X#2` are *not* migrated; when
    an incoming row's natural key has more than one stored sibling (any of `X`, `X#<n>`,
    `X#h…`), `_match_link` picks the sibling whose stored stable fields (`_stable_signature`
    over `normalised`) equal the row's, falling back to the exact key. So the first load after
    the change updates the existing rows in place under their old keys and creates nothing.
  - **Change-event identity** (audit §3.1 item 2). `event.idempotency_key` is
    `source:record_id:event_type:field:before_hash:after_hash:observed_at`. Re-loading the same
    snapshot is a no-op (same observation time, same before/after); a status that returns to an
    earlier value, or a record removed a second time after being re-sighted, is a new event
    because the before-state and/or the observation time differ. `removed` events name a record
    that is no longer in the frame, so their subject is resolved through the stored link for the
    event's `record_id` (previously they were skipped as "unknown record_id" and never written);
    a record seen again clears its link's `gone_at`.
  - **Field provenance** (audit §3.1 item 4): `field_provenance[field]` is re-stamped with the
    run's `retrieved_at`/`source_id`/`licence_id` whenever that field's value changes, and kept
    when it does not (previously written once at creation and never touched again).
  - **Organisation slug/punctuation collisions.** Two raw sponsor spellings that normalise to the
    same organisation once case, whitespace and punctuation are stripped (e.g. "CED Development,
    Inc." vs "Ced Development Inc") resolve to one `organization` row; every distinct raw spelling
    seen is kept as its own `organization_alias` row (docs/21 §3.6) rather than raising a
    `UNIQUE constraint failed: organization.slug` error. Two names that differ in letters (not
    just punctuation) still become two organisations, per `services/resolve/merge.py`'s separate,
    heavier corp-suffix-stripping fuzzy pass for anything beyond that.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import pathlib
import re
import uuid as _uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.dedupe import (
    CONTENT_SUFFIX_RE,
    content_disambiguator,
    raw_disambiguator,
    split_key,
)
from pipeline.connectors.registry import GATED_REUSE, Registry, SourceEntry
from services.db.models import (
    Event,
    Licence,
    Location,
    Opportunity,
    OpportunitySource,
    Organization,
    OrganizationAlias,
    Proposal,
    ProposalSource,
    Source,
    SourceRun,
    new_uuid,
)
from services.ids import public_id, slugify
from services.ingest.geocode import CountyGazetteer, default_gazetteer, geocode
from services.ingest.lag import change_event_public_at, record_public_at

#: Default rows-per-flush for `load_dataframe`'s bulk-insert pass (Sprint 3, services/README.md
#: "Bulk-insert pass (Sprint 3)"): every id used inside one call (`proposal`/`organization`/
#: `location`/`proposal_source` primary keys) is generated client-side (`new_uuid()`, the same
#: callable the ORM column `default=` already used, just invoked before `session.add` instead of
#: at flush time) so a row's dependents can be wired up without a flush to learn its id. This
#: batch size only bounds how often the *pending* INSERTs are actually sent to the database — it
#: changes no id, no dedupe key and no write; a smaller or larger value produces byte-identical
#: rows, just in a different number of round trips. `Event.seq` is the one exception (see the
#: `_assign_event_seq` docstring in services/db/models.py) and is deliberately flushed one row at
#: a time regardless of this setting.
DEFAULT_BATCH_SIZE = 500

#: A `reuse: attribution` source whose recorded terms explicitly call it out as derived-only
#: (docs/00-PLAN.md's 2026-09-12 legal register: "CAISO and NYISO derived-only with credit") is a
#: per-source override, not a blanket property of the `attribution` reuse class -- docs/21 §8's
#: general `attribution` row is "everything, at lag, with credit" (raw allowed); only the specific
#: sources whose licence text says so withhold raw. `data/sources.yaml` has no dedicated boolean
#: for this yet (a data-engineer follow-up, services/README.md open decision #11), so this reads
#: the signal that is already there: the free-text `notes`/`license` clause a data-engineer wrote
#: for exactly this purpose (CAISO: "Publish derived-only until counsel resolves..."; NYISO:
#: "Derived-only at launch with credit ... raw-ok candidate after counsel sign-off"). A source
#: whose terms don't say "derived-only" (e.g. GB NESO, credited but raw-ok) is unaffected.
_DERIVED_ONLY_RE = re.compile(r"derived[\s-]only", re.I)

log = logging.getLogger(__name__)

#: data/sources.yaml `publication` values (docs/21 §8; scripts/check_manifest_licences.py rejects
#: any other value and any value more permissive than the legal register allows).
PUBLICATION_VALUES = ("raw_ok", "derived_only", "none")


def _is_derived_only_override(entry: SourceEntry) -> bool:
    """Explicit `publication: derived_only` wins (blockers sprint, 2026-09-19); the notes/licence
    regex above survives only as a warned fallback for an entry that predates the field."""
    if entry.publication is not None:
        if entry.publication not in PUBLICATION_VALUES:
            raise GateRefused(f"{entry.id}: publication={entry.publication!r} not in {PUBLICATION_VALUES}")
        return entry.publication == "derived_only"
    if entry.reuse != "attribution":
        return False
    matched = bool(_DERIVED_ONLY_RE.search(entry.notes or "") or _DERIVED_ONLY_RE.search(entry.license or ""))
    log.warning(
        "%s: no `publication` field in data/sources.yaml; regex fallback (%s)",
        entry.id,
        "matched" if matched else "no match",
    )
    return matched


Kind = Literal["proposal", "opportunity"]

#: pipeline/diff.py event_type -> docs/21 §7.3 event_type vocabulary.
DIFF_EVENT_TYPE_MAP: dict[str, str] = {
    "new": "created",
    "status_change": "status_change",
    "withdrawn": "withdrawn",
    "capacity_change": "capacity_changed",
    "cod_change": "field_changed",
    "removed": "withdrawn",
}


class GateRefused(Exception):
    """The source's registry reuse class is `restricted` or `unknown` (docs/21 §8, CLAUDE.md)."""


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class LoadResult:
    source_run_id: _uuid.UUID
    proposals_created: int = 0
    proposals_updated: int = 0
    opportunities_created: int = 0
    opportunities_updated: int = 0
    events_created: int = 0
    events_skipped_idempotent: int = 0
    locations_created: int = 0
    locations_exact_promoted: int = 0
    organizations_created: int = 0
    warnings: list[str] = field(default_factory=list)


def _bump_dq_status(run: SourceRun, level: str) -> None:
    """Escalate `run.dq_status` to at least `level`, never downgrading a worse one already
    recorded (docs/20 §10/§12 vocabulary: `pass < warn < fail`)."""
    order = {"pass": 0, "warn": 1, "hold": 2, "fail": 2}
    current = run.dq_status or "pass"
    if order.get(level, 0) > order.get(current, 0):
        run.dq_status = level


def _record_dq_warning(run: SourceRun | None, *, check: str, detail: str, data: dict[str, Any]) -> None:
    """Append one warning onto `source_run.dq` (docs/21 §4.2), in the same `{check, level, detail,
    data}` shape `pipeline.connectors.dq.Check` already uses, so admin tooling reading this column
    sees one consistent structure whether the warning came from the connector's own DQ gates or
    this independent, loader-side check. A no-op when the caller has no `SourceRun` to attach to
    (e.g. a direct `load_dataframe` call in a test) -- the warning still reaches the caller via
    `LoadResult.warnings`."""
    if run is None:
        return
    existing = run.dq if isinstance(run.dq, dict) else {}
    checks = [*existing.get("checks", []), {"check": check, "level": "warn", "detail": detail, "data": data}]
    run.dq = {**existing, "checks": checks, "status": existing.get("status") or "warn"}
    _bump_dq_status(run, "warn")


def _assert_not_gated(entry: SourceEntry) -> None:
    """Independent re-check of the connector's own gate (see module docstring)."""
    if entry.reuse in GATED_REUSE:
        raise GateRefused(
            f"{entry.id} has reuse={entry.reuse!r}; the loader refuses to ingest it regardless of "
            "where the file came from (docs/21 §8, CLAUDE.md)"
        )


def upsert_licence_and_source(session: Session, entry: SourceEntry, manifest_version: str) -> Source:
    """Mirror one `data/sources.yaml` entry into `licence` + `source` (docs/21 §4.1).

    Both gate checks must hold: the caller re-verifies `entry.reuse` before calling this (see
    `_assert_not_gated`), and this function refuses a second time on the licence row it is about
    to write, so a bug in the caller cannot silently widen the gate.
    """
    _assert_not_gated(entry)
    if entry.publication == "none":
        raise GateRefused(f"{entry.id}: publication=none publishes nothing (docs/21 §8)")
    licence_id = entry.licence_id
    is_open_or_attribution = entry.reuse in ("open", "attribution")
    if not is_open_or_attribution:
        raise GateRefused(f"{entry.id}: reuse={entry.reuse!r} is not publishable")

    licence = session.get(Licence, licence_id)
    now = utcnow()
    derived_only = _is_derived_only_override(entry)
    if licence is None:
        licence = Licence(
            id=licence_id,
            name=f"{entry.name} terms of use",
            reuse_class=entry.reuse,
            attribution_required=entry.reuse == "attribution",
            attribution_text=(
                f"Source: {entry.operator or entry.name}" if entry.reuse == "attribution" else None
            ),
            requires_link_back=entry.reuse == "attribution",
            allows_derived_publication=True,
            # docs/21 §8: `open` is always raw-ok; `attribution` is raw-ok too unless this
            # source's own terms are recorded as a derived-only override (module docstring above)
            # -- never hardcoded True regardless of reuse class (was services/README.md open
            # decision #11).
            allows_raw_publication=not derived_only,
            allows_api_redistribution=True,
            allows_bulk_export=True,
            allows_commercial_use=entry.reuse == "open",
            share_alike=False,
            gate_flag=False,
            evidence_url=entry.url,
            evidence_retrieved_at=now,
            classified_by="data-engineer",
            notes=entry.notes or None,
            quote_text=entry.license or None,
        )
        session.add(licence)
        session.flush()
    if licence.reuse_class in GATED_REUSE:  # the "both must hold" re-check
        raise GateRefused(f"{entry.id}: stored licence {licence.id} is gated ({licence.reuse_class})")

    source = session.get(Source, entry.id)
    manifest_hash = hashlib.sha256(json.dumps(entry.raw, sort_keys=True, default=str).encode()).hexdigest()
    if source is None:
        source = Source(
            id=entry.id,
            name=entry.name,
            jurisdiction=entry.jurisdiction or None,
            category=entry.category,
            operator=entry.operator or None,
            url=entry.url,
            access=entry.access,
            format=entry.format or None,
            cadence=entry.cadence,
            tier=entry.tier,
            effort=entry.effort or None,
            egress=entry.egress,
            connector=entry.connector or None,
            implemented=entry.implemented,
            licence_id=licence.id,
            # docs/21 §5.4's source_permits gate requires publish_state = 'public' exactly (not
            # 'api_only') before the public tier shows anything from this source. Previously
            # hardcoded 'api_only' regardless of registry class, pushed onto a manual admin-style
            # flip (services/README.md open decision #5, `web/data_loading.py::
            # _flip_publish_state_public`'s workaround for the missing admin surface). By this
            # point in the function `entry.reuse` is already known to be `open`/`attribution` --
            # `_assert_not_gated` and the `is_open_or_attribution` check above both raise first --
            # so the registry class now decides directly: a source that cleared the licence gate
            # loads straight to public, matching what an admin publish action would do (the same
            # call the workaround made explicit); `restricted`/`unknown` never reach here, but the
            # branch is written to fail closed (`ingest_only`) rather than assume that continues
            # to hold.
            publish_state="public" if entry.reuse in ("open", "attribution") else "ingest_only",
            # The change-event delay, declared per source in the manifest (owner, 2026-09-19,
            # paywall by shape). `source.lag_days` no longer delays the *record* -- nothing
            # reads it for a proposal or an opportunity any more -- it is the number of days a
            # change event from this source waits before the public tier sees it
            # (`services/ingest/lag.py`). Seeded on create only: once the row exists, an
            # operator's audited `PATCH /admin/v1/sources/{id}` is the authority, so a manifest
            # re-sync never silently reverts a deliberate runtime change.
            lag_days=entry.change_event_lag_days,
            host=entry.host or None,
            max_rps=entry.max_rps,
            manifest_version=manifest_version,
            manifest_hash=manifest_hash,
        )
        session.add(source)
    else:
        source.implemented = entry.implemented
        source.manifest_version = manifest_version
        source.manifest_hash = manifest_hash
        source.licence_id = licence.id
    session.flush()
    return source


def _row_get(row: Mapping[str, Any], key: str) -> Any:
    v = row.get(key)
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if v is pd.NaT or v is pd.NA:
        return None
    return v


def _to_datetime(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    resolved: dt.datetime = ts.to_pydatetime()
    return resolved


def _to_date(value: Any) -> dt.date | None:
    ts = _to_datetime(value)
    return ts.date() if ts else None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


#: Case/whitespace/punctuation-only normalisation for organisation matching (task scope: not the
#: corp-suffix stripping `pipeline.normalize.norm_org` does for `services/resolve/merge.py`'s
#: separate, heavier fuzzy pass over already-loaded organisations).
_ORG_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def _org_punct_key(text: str) -> str:
    return _ORG_PUNCT_RE.sub(" ", text.strip().lower()).strip()


@dataclass
class _LoadCache:
    """Everything one `load_dataframe` call would otherwise re-query per row, loaded once up front
    (Sprint 3 bulk-insert pass; `services/README.md` "Bulk-insert pass (Sprint 3)" has the
    before/after measurement and the profile that motivated each field below).

    Every lookup this module used to run as a per-row `session.scalar(select(...))` — the active
    `proposal_source`/`opportunity_source` link for a natural key, the entity a link points at, an
    organisation by exact or punctuation-normalised spelling, whether an alias is already on file,
    whether a slug is taken — is a dict/set membership test against one of these instead. Nothing
    here changes *which* row wins a lookup, only how many round trips finding it costs: each field
    is seeded from the same query the old per-row code ran (just once, not N times) and kept in
    sync as this call creates new rows, so a later row in the same dataframe sees an earlier row's
    new organisation/link exactly as it would have via a flushed, re-queried session.
    """

    #: `source_record_id -> ProposalSource | OpportunitySource`, restricted to this `source` and
    #: `active`, i.e. the same set the old code re-selected on every row.
    links: dict[str, Any]
    #: natural key (the stored key with any `#…` suffix stripped) -> every active link sharing
    #: it, for the legacy/content match in `_match_link` (module docstring, "Legacy keys on read").
    siblings: dict[str, list[Any]]
    #: entity primary key -> `Proposal | Opportunity`, for every entity the links above point at,
    #: plus every entity this call creates.
    entities: dict[_uuid.UUID, Any]
    #: entity primary key -> its active link, kept for the `diff_type == "removed"` branch below
    #: (the old code's `select(link_cls).where(getattr(link_cls, fk_name) == subject_id, ...)`).
    links_by_entity: dict[_uuid.UUID, Any]
    #: `organization.name_normalised` (exact, case-folded) -> `Organization`, over *every*
    #: organisation regardless of `merged_into_id` — matches the old exact-match query's scope.
    org_by_exact: dict[str, Organization]
    #: `_org_punct_key(name_canonical)` -> `Organization`, over organisations with
    #: `merged_into_id is None` only — matches the old punctuation-match query's scope.
    org_by_punct: dict[str, Organization]
    #: `(organization_id, alias_normalised)` pairs already on file, any organisation.
    alias_keys: set[tuple[_uuid.UUID, str]]
    #: every `organization.slug` already taken, any organisation — the old collision re-check's scope.
    existing_slugs: set[str]
    #: one gazetteer instance for the whole call instead of a `default_gazetteer()` cache check
    #: per row (`services/ingest/geocode.py` already caches it process-wide; this just avoids
    #: paying that lookup 14,000+ times).
    gaz: CountyGazetteer


def _build_load_cache(
    session: Session, source: Source, entity_cls: type[Any], link_cls: type[Any], fk_name: str
) -> _LoadCache:
    links: dict[str, Any] = {
        link.source_record_id: link
        for link in session.scalars(
            select(link_cls).where(link_cls.source_id == source.id, link_cls.active.is_(True))
        )
    }
    links_by_entity: dict[_uuid.UUID, Any] = {getattr(link, fk_name): link for link in links.values()}
    siblings: dict[str, list[Any]] = {}
    for key, link in links.items():
        siblings.setdefault(split_key(key)[0], []).append(link)
    entities: dict[_uuid.UUID, Any] = {}
    if links_by_entity:
        entities = {
            entity.id: entity
            for entity in session.scalars(select(entity_cls).where(entity_cls.id.in_(links_by_entity)))
        }

    org_by_exact: dict[str, Organization] = {}
    org_by_punct: dict[str, Organization] = {}
    existing_slugs: set[str] = set()
    for org in session.scalars(select(Organization)):
        existing_slugs.add(org.slug)
        org_by_exact.setdefault(org.name_normalised, org)
        if org.merged_into_id is None:
            org_by_punct.setdefault(_org_punct_key(org.name_canonical), org)

    alias_keys: set[tuple[_uuid.UUID, str]] = {
        (organization_id, alias_normalised)
        for organization_id, alias_normalised in session.execute(
            select(OrganizationAlias.organization_id, OrganizationAlias.alias_normalised)
        )
    }

    return _LoadCache(
        links=links,
        siblings=siblings,
        entities=entities,
        links_by_entity=links_by_entity,
        org_by_exact=org_by_exact,
        org_by_punct=org_by_punct,
        alias_keys=alias_keys,
        existing_slugs=existing_slugs,
        gaz=default_gazetteer(),
    )


def _flush_pending(session: Session, pending_links: list[Any]) -> None:
    """Flush everything the row loop has added so far — entities, organisations, locations,
    aliases, and any dirty updates to an existing entity/link — then, only once that has actually
    reached the database, add and flush `pending_links` (the `ProposalSource`/`OpportunitySource`
    rows created alongside them).

    Two ordered flushes, not one: `ProposalSource`/`OpportunitySource` carry no ORM
    `relationship()` back to `Proposal`/`Opportunity` for SQLAlchemy's unit-of-work to infer
    insert order from (only the reverse, `Proposal.sources`, and that one is `viewonly=True` --
    deliberately excluded from flush-dependency tracking, docs/21 §3.1/§3.2), so a link and its
    entity flushed in the same statement batch can be sent in either order and the link's
    `proposal_id`/`opportunity_id` foreign key trips if it lands first. New entities do carry a
    real (non-viewonly) `relationship()` to `Organization`/`Location`, so those two are safe to
    flush together with the entity in the first call. Called at every `batch_size` boundary and
    once more after the loop -- `pending_links` accumulates across the whole `load_dataframe` call
    (not reset except here), so a call with fewer than `batch_size` new rows still gets both
    flushes exactly once, at the end.
    """
    session.flush()
    if pending_links:
        session.add_all(pending_links)
        session.flush()
        pending_links.clear()


def _add_organization_alias_if_new(
    cache: _LoadCache,
    session: Session,
    org: Organization,
    *,
    alias: str,
    source: Source,
    source_url: str,
    retrieved_at: dt.datetime,
) -> None:
    """Record one raw spelling as an `organization_alias` row (docs/21 §3.6), skipping it if this
    exact spelling is already on file for this organisation (re-running the same source/run must
    not duplicate the alias) — checked against `cache.alias_keys` rather than a per-row `SELECT`."""
    alias_normalised = alias.lower()
    key = (org.id, alias_normalised)
    if key in cache.alias_keys:
        return
    session.add(
        OrganizationAlias(
            organization_id=org.id,
            alias=alias,
            alias_normalised=alias_normalised,
            kind="filing_spelling",
            source_id=source.id,
            source_url=source_url,
            retrieved_at=retrieved_at,
            licence_id=source.licence_id,
            confidence=1.0,
            created_by="pipeline",
        )
    )
    cache.alias_keys.add(key)


def _get_or_create_organization(
    cache: _LoadCache,
    session: Session,
    name: str | None,
    *,
    source: Source,
    source_url: str,
    retrieved_at: dt.datetime,
) -> tuple[Organization | None, bool]:
    """Find or create the organisation for one raw sponsor spelling.

    Matching is two-tier: an exact (case-folded) spelling match is tried first — this is the
    original, unchanged fast path and keeps compatibility with any organisation row already keyed
    on plain `name.lower()` (e.g. `services/resolve/report.py`'s pre-seeding). Only on a miss do we
    fall back to `_org_punct_key`, which additionally strips whitespace/punctuation, so that two
    spellings differing only in punctuation land on the same organisation (task scope) instead of
    tripping the `organization.slug` unique constraint (`services/resolve/README.md`'s observed
    limitation). Every raw spelling that resolves to an existing organisation via either path is
    recorded as an `organization_alias` row (docs/21 §3.6), including the spelling an organisation
    was first created from, so two punctuation-only variants leave the org with two aliases.

    Both matches and the slug-collision re-check below read `cache` (`_LoadCache`, built once per
    `load_dataframe` call) instead of issuing a `SELECT` for every row — the bulk-insert pass; the
    matching logic itself, including which organisation wins when both an exact and punctuation
    match exist, is unchanged.
    """
    if not name or not str(name).strip():
        return None, False
    raw_name = str(name).strip()
    exact_key = raw_name.lower()

    org = cache.org_by_exact.get(exact_key)
    if org is not None:
        return org, False

    punct_key = _org_punct_key(raw_name)
    candidate = cache.org_by_punct.get(punct_key)
    if candidate is not None:
        _add_organization_alias_if_new(
            cache,
            session,
            candidate,
            alias=raw_name,
            source=source,
            source_url=source_url,
            retrieved_at=retrieved_at,
        )
        return candidate, False

    org_id = new_uuid()
    org_public_id = public_id("org", org_id)
    slug = slugify(raw_name)
    if slug in cache.existing_slugs:
        # Defence in depth: two letter-distinct names should never coincidentally collide once
        # `_org_punct_key` above has already ruled out a punctuation-only match, but a lowest-cost
        # deterministic suffix here means a bug in that reasoning fails safe (no row, no crash)
        # rather than raising `UNIQUE constraint failed: organization.slug` at ingestion.
        slug = f"{slug}-{org_public_id[-6:].lower()}"
    org = Organization(
        id=org_id,
        public_id=org_public_id,
        slug=slug,
        name_canonical=raw_name,
        name_normalised=exact_key,
        type="other",
        country="US",
    )
    session.add(org)
    cache.existing_slugs.add(slug)
    cache.org_by_exact[exact_key] = org
    cache.org_by_punct.setdefault(punct_key, org)
    _add_organization_alias_if_new(
        cache, session, org, alias=raw_name, source=source, source_url=source_url, retrieved_at=retrieved_at
    )
    return org, True


def _jurisdiction(state: str | None, source_jurisdiction: str) -> str:
    if state and len(state) == 2 and state.isalpha():
        country = source_jurisdiction.split("-")[0] if "-" in source_jurisdiction else "US"
        return f"{country}-{state.upper()}"
    return source_jurisdiction or "US"


#: World bounds a real coordinate must fall inside (docs/21 §3.7 `exact`: a real point, not merely
#: a numeric-looking one) -- `(0, 0)` is excluded separately below since it is the common
#: placeholder an unset/blank source field serialises to, not a real location off the coast of
#: West Africa.
_WORLD_LAT_RANGE = (-90.0, 90.0)
_WORLD_LON_RANGE = (-180.0, 180.0)


def _extract_exact_point(raw_payload: Mapping[str, Any]) -> tuple[float, float] | None:
    """A real coordinate pair straight from the source's own raw payload (docs/04 D-8: `exact`
    outranks a county/state centroid) -- EIA-860M's raw `Latitude`/`Longitude` keys today, verified
    against `pipeline/connectors/us_eia_860m/connector.py`'s `parse()` output (the Planned sheet's
    own column names, carried through unchanged onto `raw` by `Connector.finalize`). Written as a
    plain key lookup rather than an EIA-specific branch so any other source whose normalised frame
    already carries the same two raw keys is promoted identically with no further loader change --
    none does yet (checked across `pipeline/connectors/*` this sprint).

    Returns `None` (falls through to county/state geocoding) unless both values are present,
    numeric, inside `_WORLD_LAT_RANGE`/`_WORLD_LON_RANGE`, and not the `(0, 0)` placeholder --
    `docs/21` §3.7's `exact` tier means a real point, not a coordinate that merely parses as one.
    """
    lat_raw = raw_payload.get("Latitude")
    lon_raw = raw_payload.get("Longitude")
    if lat_raw is None or lon_raw is None:
        return None
    try:
        lat = float(lat_raw)
        lon = float(lon_raw)
    except (TypeError, ValueError):
        return None
    if pd.isna(lat) or pd.isna(lon):
        return None
    if lat == 0.0 and lon == 0.0:
        return None
    if not (_WORLD_LAT_RANGE[0] <= lat <= _WORLD_LAT_RANGE[1]):
        return None
    if not (_WORLD_LON_RANGE[0] <= lon <= _WORLD_LON_RANGE[1]):
        return None
    return (lon, lat)


def _get_or_create_location(
    session: Session,
    *,
    state: str | None,
    county: str | None,
    source: Source,
    retrieved_at: dt.datetime,
    derived_only: bool,
    raw_payload: Mapping[str, Any] | None = None,
    gaz: CountyGazetteer | None = None,
) -> Location | None:
    """A row's `Location`: a real coordinate from its raw payload when one validates (`exact`,
    docs/04 D-8), else a geocoded county centroid, else a state centroid, else unplaced
    (docs/21 §3.7) -- `services/ingest/geocode.py` does that county/state lookup against the
    vendored public-domain county-centroid table so this no longer depends on `web/`'s copy of the
    same geocoder (services/README.md open decision #2).

    `derived_only` (the source's licence withholds raw/exact geo, docs/21 §8's "attribution, raw
    withheld" row, `_is_derived_only_override` above) is checked *before* `_extract_exact_point`
    runs at all: docs/04 D-9 is explicit that an exact coordinate is a raw field like any other, so
    a derived-only source never gets promoted regardless of what its raw payload carries, and keeps
    falling through to the county/state centroid with `precision_reason = "licence"` stamped (so
    the API can render the restricted-precision note) exactly as before this promotion existed.

    `county_fips` (docs/21 §3.7, added 2026-09-15) is looked up from the row's own `state`/`county`
    strings against the same vendored gazetteer `geocode()` uses -- never derived from `point`,
    exact or geocoded: there is no point-in-polygon here, so a coordinate alone never fills this
    column (docs/21 §3.7). That makes the lookup independent of which branch below produced the
    `Location`: a `county_centroid` row gets it from the same county name `geocode()` just resolved,
    and an `exact` row gets it too when the source row happens to name a real county alongside its
    coordinate (EIA-860M does). A GB row (`country_code == "US"` is false) never gets one -- `county`
    there is a substation name, not a US county, and FIPS is a US-only key.
    """
    country_code = "GB" if (source.jurisdiction or "").upper().startswith("GB") else "US"
    county_fips: str | None = None
    if country_code == "US":
        county_fips = (gaz if gaz is not None else default_gazetteer()).county_fips(state, county)
    exact_point = None if derived_only else _extract_exact_point(raw_payload or {})
    if exact_point is not None:
        kind = "point"
        point: tuple[float, float] | None = exact_point
        precision = "exact"
        geocoder: str | None = "source_provided"
    else:
        if not state and not county:
            return None
        kind = "county" if county else "state"
        # GB rows (the NESO TEC register) carry a transmission "Connection Site" in `county` and
        # no state; `geocode` resolves it against the vendored substation gazetteer when told the
        # country (services/ingest/geocode.py, Sprint 3 item 5). A hit is a substation-level
        # proxy, stamped `gb_substation` so the API and the map can say so (docs/21 §3.7).
        point, precision = geocode(state, county, gaz=gaz, country=country_code)
        geocoder = "gb_substation" if country_code == "GB" and point is not None else None
    loc = Location(
        id=new_uuid(),
        kind=kind,
        geom=point,
        precision=precision,
        precision_reason="licence" if derived_only else None,
        county_fips=county_fips,
        county_name=county or None,
        state_code=(f"US-{state.upper()}" if state and country_code == "US" else None),
        country=country_code,
        geocoder=geocoder,
        source_id=source.id,
        source_url=source.url,
        retrieved_at=retrieved_at,
        licence_id=source.licence_id,
    )
    session.add(loc)
    return loc


def _proposal_fields_from_row(row: Mapping[str, Any], source: Source) -> dict[str, Any]:
    queue_id = _row_get(row, "queue_id")
    identifiers: dict[str, Any] = {}
    if queue_id:
        identifiers["queue_ids"] = [{"iso": _row_get(row, "iso") or "", "id": str(queue_id)}]
    if _row_get(row, "eia_plant_id"):
        identifiers["eia_plant_id"] = str(_row_get(row, "eia_plant_id"))
    if _row_get(row, "eia_generator_id"):
        identifiers["eia_generator_id"] = str(_row_get(row, "eia_generator_id"))

    name = (
        _row_get(row, "name_canonical")
        or _row_get(row, "sponsor_name")
        or _row_get(row, "queue_id")
        or "Untitled"
    )
    lifecycle_state = _row_get(row, "lifecycle_state") or "unknown"
    return {
        "kind": _row_get(row, "kind") or "other",
        "name_canonical": str(name),
        "technology": _row_get(row, "technology"),
        "technology_raw": _row_get(row, "technology_raw"),
        "capacity_mw": _to_float(_row_get(row, "capacity_mw")),
        "storage_mwh": _to_float(_row_get(row, "storage_mwh")),
        "jurisdiction": _jurisdiction(_row_get(row, "state"), source.jurisdiction or "US"),
        "iso": _row_get(row, "iso"),
        "lifecycle_state": lifecycle_state,
        "status_raw": _row_get(row, "status_raw"),
        "identifiers": identifiers,
        "proposed_online_date": _to_date(_row_get(row, "proposed_cod")),
    }


def _opportunity_fields_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    identifiers = _row_get(row, "identifiers") or {}
    if isinstance(identifiers, str):
        try:
            identifiers = json.loads(identifiers)
        except (json.JSONDecodeError, TypeError):
            identifiers = {}
    technologies_raw = _row_get(row, "technologies")
    if isinstance(technologies_raw, str):
        technologies = [t for t in re_split(technologies_raw) if t]
    elif isinstance(technologies_raw, (list, tuple)):
        technologies = list(technologies_raw)
    else:
        technologies = []
    return {
        "kind": _row_get(row, "kind") or "program",
        "title": str(_row_get(row, "title") or "Untitled"),
        "summary": _row_get(row, "summary"),
        "jurisdiction": _row_get(row, "jurisdiction") or "US",
        "technologies": technologies,
        "capacity_sought_mw": _to_float(_row_get(row, "capacity_sought_mw")),
        "budget_amount": _to_float(_row_get(row, "budget_amount")),
        "budget_currency": _row_get(row, "budget_currency"),
        "open_at": _to_date(_row_get(row, "open_at")),
        "due_at": _to_datetime(_row_get(row, "due_at")),
        "status": _row_get(row, "status") or "unknown",
        "status_raw": _row_get(row, "status_raw"),
        "identifiers": identifiers,
    }


def re_split(value: str) -> list[str]:
    return [v.strip() for v in value.replace(";", ",").split(",")]


def _jsonable(value: Any) -> Any:
    """Recursively convert `date`/`datetime` to ISO text so a dict is safe for the `jsonb`/`JSON`
    columns (`normalised`, `field_provenance`) regardless of dialect JSON serializer."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _parse_raw(raw_value: Any) -> dict[str, Any]:
    if isinstance(raw_value, dict):
        return raw_value
    if isinstance(raw_value, str) and raw_value:
        try:
            parsed: dict[str, Any] = json.loads(raw_value)
            return parsed
        except json.JSONDecodeError:
            return {}
    return {}


_STABLE_SIGNATURE_FIELDS: dict[str, tuple[str, ...]] = {
    "proposal": ("name_canonical", "capacity_mw", "technology", "jurisdiction"),
    "opportunity": ("title", "capacity_sought_mw", "jurisdiction", "kind"),
}


def _sig_norm(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.6g}"
    text = str(value).strip().lower()
    try:
        return f"{float(text):.6g}"
    except ValueError:
        return text


def _stable_signature(fields: Mapping[str, Any], kind: Kind) -> tuple[str, ...]:
    """The stable fields a row and a stored link are compared on when one natural key has
    several stored siblings (module docstring, "Legacy keys on read"). Status and dates are
    excluded on purpose: they change over a record's life without changing its identity."""
    return tuple(_sig_norm(fields.get(name)) for name in _STABLE_SIGNATURE_FIELDS[kind])


def _wanted_key(
    source: Source, raw_source_record_id: str, record_id: str, row: Mapping[str, Any], *, in_dup_group: bool
) -> str:
    """The stored `source_record_id` this row asks for: the connector's own content suffix when
    its `record_id` carries one, else a freshly computed one when the natural key is shared by
    several distinct `record_id`s in this call, else the bare natural key. A positional `#<n>`
    on an incoming `record_id` (an old parquet re-loaded) is never trusted."""
    prefix = f"{source.id}:"
    if record_id.startswith(prefix):
        m = CONTENT_SUFFIX_RE.match(record_id[len(prefix) :])
        if m is not None and m.group("base") == raw_source_record_id:
            return f"{raw_source_record_id}#{m.group('suffix')}"
    if in_dup_group:
        return f"{raw_source_record_id}#{content_disambiguator(row)}"
    return raw_source_record_id


def _unclaimed_key(wanted: str, base: str, raw: str, claimed: Mapping[str, str]) -> str:
    """`wanted` unless another `record_id` in this frame already holds it, in which case the same
    fallback chain `pipeline.connectors.dedupe.suffix_duplicates` uses: a hash of the raw row,
    then an order suffix (`~2`, `~3`, ...) among byte-identical rows. Two rows whose stable
    fields *and* raw payload are identical are interchangeable by definition, so the order is
    harmless; what matters is that both load instead of the second violating the unique key
    (`proposal_source.source_id, source_record_id`), which is what the 2026-09-12 eval fixture's
    repeated "Untitled" NYISO rows did on CI on 2026-09-19."""
    if wanted not in claimed:
        return wanted
    candidate = f"{base}#{raw_disambiguator(raw)}" if raw else wanted
    if candidate not in claimed:
        return candidate
    n = 2
    while f"{candidate}~{n}" in claimed:
        n += 1
    return f"{candidate}~{n}"


def _match_link(
    cache: _LoadCache,
    wanted: str,
    base: str,
    fields: Mapping[str, Any],
    kind: Kind,
    claimed: Mapping[str, str],
) -> Any | None:
    """The stored link for `wanted`, accepting legacy spellings: an exact hit wins when the
    natural key has no other stored sibling; otherwise the unclaimed sibling whose stable fields
    equal the row's wins (so `X`/`X#2` rows written by the positional scheme, and `X` rows that
    later became `X#h…` or vice versa, keep their identity), with the exact hit as fallback."""
    exact = cache.links.get(wanted)
    if exact is not None and exact.source_record_id in claimed:
        exact = None
    group = [link for link in cache.siblings.get(base, []) if link.source_record_id not in claimed]
    if exact is not None and len(group) <= 1:
        return exact
    if not group:
        return exact
    signature = _stable_signature(fields, kind)
    matches = sorted(
        (link for link in group if _stable_signature(link.normalised or {}, kind) == signature),
        key=lambda link: str(link.source_record_id),
    )
    if matches:
        return matches[0]
    return exact


def _link_for_event_record_id(cache: _LoadCache, source: Source, record_id: str) -> Any | None:
    """A stored link for an event's `record_id` when the record is not in this frame (a
    `removed` event): the connector key with the source prefix stripped is the stored key."""
    prefix = f"{source.id}:"
    tail = record_id[len(prefix) :] if record_id.startswith(prefix) else record_id
    return cache.links.get(tail)


def _short_hash(value: Any) -> str:
    return hashlib.sha1(  # noqa: S324 — identity key, not security
        json.dumps(value, default=str, sort_keys=True).encode()
    ).hexdigest()[:12]


def load_dataframe(
    session: Session,
    source: Source,
    kind: Kind,
    records_df: pd.DataFrame,
    events_df: pd.DataFrame | None,
    *,
    run: SourceRun | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> LoadResult:
    """Upsert one connector run's normalised records and diff events (idempotent).

    `source` must already be loaded via `upsert_licence_and_source` (its gate re-checked there).
    Safe to call twice with the same `records_df`/`events_df`: `proposal_source`/
    `opportunity_source` rows are keyed on `(source_id, source_record_id)` and `event` rows on
    `idempotency_key`, so a re-run changes nothing (docs/20 §3, docs/21 §6.1).

    `batch_size` (Sprint 3 bulk-insert pass, `services/README.md` "Bulk-insert pass (Sprint 3)"):
    the record loop below reads and writes entirely through `_LoadCache` (one set of preloaded
    dicts, built once from the database) and generates every id it needs client-side, so nothing
    inside the loop depends on a flush to see an earlier row's write — `session.flush()` only
    happens every `batch_size` rows (plus once at the end), purely to bound how much stays pending
    in memory and to send writes to the database periodically. A smaller or larger value changes
    only that cadence: the rows written, their ids, and every dedupe/idempotency key are identical
    for any `batch_size >= 1` (pinned by `test_loader.py`'s digest-equality test). The event loop
    is unaffected — `Event.seq`'s `before_insert` listener (`services/db/models.py`) computes
    `MAX(seq)+1` per row and collides if two new events are flushed together, so events are still
    flushed one at a time regardless of this setting, exactly as before.
    """
    now = utcnow()
    result = LoadResult(source_run_id=run.id if run else _uuid.UUID(int=0))
    record_id_to_internal: dict[str, _uuid.UUID] = {}
    #: connector `record_id` -> the stored key resolved for it in this call. A dataframe covering
    #: more than one historical pull for the same source (as `services/resolve/report.py`'s
    #: evaluation snapshot does) legitimately repeats the *same* `record_id` many times for one
    #: natural key -- each repeat is an ordinary sequential update and must keep using the same
    #: stored key. Only a *different* `record_id` sharing that natural key is true intra-run id
    #: reuse (docs/22 §5/§7.1), and every member of such a group is keyed by content (module
    #: docstring) — never by its position in the frame.
    key_by_record_id: dict[str, str] = {}
    #: stored key -> the `record_id` that claimed it in this call: a second, different
    #: `record_id` can never be merged into the same link.
    claimed: dict[str, str] = {}

    entity_cls: type[Any] = Proposal if kind == "proposal" else Opportunity
    link_cls: type[Any] = ProposalSource if kind == "proposal" else OpportunitySource
    fk_name = "proposal_id" if kind == "proposal" else "opportunity_id"

    cache = _build_load_cache(session, source, entity_cls, link_cls, fk_name)
    records = records_df.to_dict("records") if len(records_df) else []
    distinct_record_ids: dict[str, set[str]] = {}
    for row in records:
        distinct_record_ids.setdefault(str(_row_get(row, "source_record_id")), set()).add(
            str(_row_get(row, "record_id"))
        )
    dup_naturals = {key for key, ids in distinct_record_ids.items() if len(ids) > 1}
    warned_reuse: set[str] = set()
    #: New `ProposalSource`/`OpportunitySource` rows, held back from `session.add` until the next
    #: `_flush_pending` call so their entity is guaranteed already flushed first (see that
    #: function's docstring for why order matters here).
    pending_links: list[Any] = []

    with session.no_autoflush:
        for i, row in enumerate(records):
            raw_source_record_id = str(_row_get(row, "source_record_id"))
            record_id = str(_row_get(row, "record_id"))
            retrieved_at = _to_datetime(_row_get(row, "retrieved_at")) or now
            raw_payload = _parse_raw(_row_get(row, "raw"))

            if kind == "proposal":
                fields = _proposal_fields_from_row(row, source)
            else:
                fields = _opportunity_fields_from_row(row)

            if record_id in key_by_record_id:
                source_record_id = key_by_record_id[record_id]
                existing_link = cache.links.get(source_record_id)
            else:
                in_dup_group = raw_source_record_id in dup_naturals
                wanted = _wanted_key(source, raw_source_record_id, record_id, row, in_dup_group=in_dup_group)
                wanted = _unclaimed_key(
                    wanted, raw_source_record_id, str(_row_get(row, "raw") or ""), claimed
                )
                existing_link = _match_link(cache, wanted, raw_source_record_id, fields, kind, claimed)
                source_record_id = (
                    str(existing_link.source_record_id) if existing_link is not None else wanted
                )
                key_by_record_id[record_id] = source_record_id
                claimed[source_record_id] = record_id
                if in_dup_group and raw_source_record_id in warned_reuse:
                    # A different connector `record_id` sharing this natural key inside the same
                    # run/dataframe -- never merged with its sibling(s); every member is stored
                    # under its content key (module docstring, docs/22 §5/§7.1).
                    warning = (
                        f"{source.id}: source_record_id {raw_source_record_id!r} reused by a "
                        f"distinct record ({record_id!r}) within this run; stored as "
                        f"{source_record_id!r} rather than merged into its sibling "
                        "(docs/22 §5, §7.1)"
                    )
                    result.warnings.append(warning)
                    _record_dq_warning(
                        run,
                        check="duplicate_source_record_id_same_run",
                        detail=warning,
                        data={
                            "source_record_id": raw_source_record_id,
                            "record_id": record_id,
                            "stored_as": source_record_id,
                        },
                    )
                warned_reuse.add(raw_source_record_id)

            if existing_link is not None:
                entity = cache.entities.get(getattr(existing_link, fk_name))
                if entity is None:
                    raise RuntimeError(
                        f"proposal_source/opportunity_source row {existing_link.id} points at a "
                        "missing entity — this is a store consistency bug, not a data error"
                    )
                provenance = dict(entity.field_provenance or {})
                for k, v in fields.items():
                    if v is not None and (k not in provenance or getattr(entity, k, None) != v):
                        provenance[k] = {
                            "source_id": source.id,
                            "licence_id": source.licence_id,
                            "retrieved_at": retrieved_at.isoformat(),
                        }
                    setattr(entity, k, v)
                entity.field_provenance = provenance  # reassigned so the JSON column is marked dirty
                entity.last_changed = now
                existing_link.raw = raw_payload
                existing_link.normalised = _jsonable(
                    {k: v for k, v in fields.items() if not isinstance(v, dict)}
                )
                existing_link.status_raw = fields.get("status_raw")
                existing_link.last_seen = retrieved_at
                existing_link.retrieved_at = retrieved_at
                existing_link.gone_at = None  # seen again: no longer gone from the register
                if kind == "proposal":
                    result.proposals_updated += 1
                else:
                    result.opportunities_updated += 1
            else:
                published_at = now
                # Paywall by shape, not by time (owner, 2026-09-19): a record is visible to a
                # free reader the moment it is published. `public_at == published_at` here; the
                # only surviving delay is on change events from an ISO queue register, applied
                # in the event loop below (`services/ingest/lag.py`).
                public_at = record_public_at(published_at)
                entity_id = new_uuid()
                entity_public_id = public_id("prop" if kind == "proposal" else "opp", entity_id)
                title = fields.get("name_canonical") or fields.get("title") or "record"
                entity = entity_cls(
                    id=entity_id,
                    public_id=entity_public_id,
                    slug=f"{slugify(title)}-{entity_public_id[-6:].lower()}",
                    publish_state="public",
                    published_at=published_at,
                    public_at=public_at,
                    min_reuse_class=source.licence.reuse_class,
                    source_count=1,
                    **fields,
                )
                if isinstance(entity, Proposal):
                    sponsor, sponsor_created = _get_or_create_organization(
                        cache,
                        session,
                        _row_get(row, "sponsor_name"),
                        source=source,
                        source_url=str(_row_get(row, "source_url") or source.url),
                        retrieved_at=retrieved_at,
                    )
                    if sponsor is not None:
                        entity.sponsor_org_id = sponsor.id
                        if sponsor_created:
                            result.organizations_created += 1
                    loc = _get_or_create_location(
                        session,
                        state=_row_get(row, "state"),
                        county=_row_get(row, "county"),
                        source=source,
                        retrieved_at=retrieved_at,
                        derived_only=not source.licence.allows_raw_publication,
                        raw_payload=raw_payload,
                        gaz=cache.gaz,
                    )
                    if loc is not None:
                        entity.location_id = loc.id
                        result.locations_created += 1
                        if loc.precision == "exact":
                            result.locations_exact_promoted += 1
                entity.field_provenance = {
                    k: {
                        "source_id": source.id,
                        "licence_id": source.licence_id,
                        "retrieved_at": retrieved_at.isoformat(),
                    }
                    for k in fields
                    if fields[k] is not None
                }
                session.add(entity)
                cache.entities[entity.id] = entity
                existing_link = link_cls(
                    **{fk_name: entity.id},
                    source_id=source.id,
                    source_record_id=source_record_id,
                    source_url=str(_row_get(row, "source_url") or source.url),
                    retrieved_at=retrieved_at,
                    licence_id=source.licence_id,
                    raw=raw_payload,
                    normalised=_jsonable({k: v for k, v in fields.items() if not isinstance(v, dict)}),
                    status_raw=fields.get("status_raw"),
                    first_seen=retrieved_at,
                    last_seen=retrieved_at,
                    link_method="deterministic_key",
                    link_confidence=1.0,
                )
                pending_links.append(existing_link)
                cache.links[source_record_id] = existing_link
                cache.siblings.setdefault(split_key(source_record_id)[0], []).append(existing_link)
                cache.links_by_entity[entity.id] = existing_link
                if kind == "proposal":
                    result.proposals_created += 1
                else:
                    result.opportunities_created += 1

            record_id_to_internal[record_id] = getattr(existing_link, fk_name)

            if (i + 1) % batch_size == 0:
                _flush_pending(session, pending_links)

    _flush_pending(session, pending_links)

    if events_df is not None and len(events_df):
        #: Preloaded once (Sprint 3 bulk-insert pass) instead of one `SELECT` per event; a key
        #: added below as each event is created keeps a later duplicate in the same call correctly
        #: idempotent without a re-query. `Event.source_id` is always this call's `source.id` for
        #: every key this loader writes (set explicitly below), so filtering on it is exact, not
        #: an approximation.
        existing_event_keys: set[str] = set(
            session.scalars(select(Event.idempotency_key).where(Event.source_id == source.id))
        )
        for ev in events_df.to_dict("records"):
            diff_type = str(ev["event_type"])
            record_id = str(ev["record_id"])
            subject_id = record_id_to_internal.get(record_id)
            if subject_id is None:
                # A `removed` record is, by definition, not in this frame: find it by its link.
                link = _link_for_event_record_id(cache, source, record_id)
                subject_id = getattr(link, fk_name) if link is not None else None
            if subject_id is None:
                result.warnings.append(f"event for unknown record_id {record_id!r} skipped")
                continue
            event_type = DIFF_EVENT_TYPE_MAP.get(diff_type, "field_changed")
            observed_at = _to_datetime(ev.get("observed_at")) or now
            field_name = ev.get("field") or "lifecycle_state"
            before_val = ev.get("before")
            after_val = ev.get("after")
            if isinstance(before_val, float) and pd.isna(before_val):
                before_val = None
            if isinstance(after_val, float) and pd.isna(after_val):
                after_val = None
            observed_token = observed_at.astimezone(dt.UTC).isoformat(timespec="seconds")
            # Before-state and observation time are part of the identity (module docstring):
            # A->B->A is two events, a second removal after a re-sighting is a second event, and
            # re-loading one snapshot (same observation time) stays a no-op.
            idempotency_key = (
                f"{source.id}:{record_id}:{event_type}:{field_name}:"
                f"{_short_hash(before_val)}:{_short_hash(after_val)}:{observed_token}"
            )

            if idempotency_key in existing_event_keys:
                result.events_skipped_idempotent += 1
                continue

            published_at = now
            # The one surviving time lever: a change event waits `source.lag_days` days on the
            # public tier when the source declares a change-event lag (the eight `us.iso.*`
            # queue registers today, seeded from `data/sources.yaml change_event_lag_days`), and
            # publishes live otherwise. `source.lag_overrides[event_type]` still wins per type.
            public_at = change_event_public_at(
                published_at,
                source_change_event_lag_days=source.lag_days,
                lag_overrides=source.lag_overrides,
                event_type=event_type,
            )
            event = Event(
                subject_type=kind,
                subject_id=subject_id,
                event_type=event_type,
                observed_at=observed_at,
                published_at=published_at,
                public_at=public_at,
                source_id=source.id,
                source_url=source.url,
                retrieved_at=observed_at,
                licence_id=source.licence_id,
                before=({field_name: before_val} if before_val is not None else None),
                after=({field_name: after_val} if after_val is not None else None),
                changed_keys=[str(field_name)],
                actor_type="pipeline",
                run_id=run.id if run else None,
                idempotency_key=idempotency_key,
            )
            session.add(event)
            # `Event.seq`'s `before_insert` listener computes `MAX(seq) + 1` from the database at
            # insert time (services/db/models.py); flushing more than one new event together would
            # let two rows compute the same `MAX(seq)` and collide on the `seq` unique constraint
            # (documented on that listener). Deliberately not batched by `batch_size` — the
            # bulk-insert pass optimises the record loop above, not this one.
            session.flush()
            existing_event_keys.add(idempotency_key)
            result.events_created += 1

            if diff_type == "removed":
                link = cache.links_by_entity.get(subject_id)
                if link is not None:
                    link.gone_at = observed_at

    return result


def load_from_files(
    session: Session,
    source_id: str,
    ts: str,
    *,
    data_root: pathlib.Path = pathlib.Path("data"),
    registry: Registry | None = None,
) -> LoadResult:
    """Read `data/normalized/<source_id>/<ts>.parquet` (+ the matching `events/` file and, if
    present, the `runs/<source_id>/<ts>.json` record) and load them (docs/20 §3.2, §3.7).

    Raises `GateRefused` before touching any file if the source's registry entry is gated —
    independent of whatever the connector run already did (module docstring).
    """
    registry = registry or Registry()
    entry = registry.get(source_id)
    _assert_not_gated(entry)

    normalized_path = data_root / "normalized" / source_id / f"{ts}.parquet"
    events_path = data_root / "events" / source_id / f"{ts}.parquet"
    run_path = data_root / "runs" / source_id / f"{ts}.json"
    if not normalized_path.exists():
        raise FileNotFoundError(normalized_path)

    records_df = pd.read_parquet(normalized_path)
    events_df = pd.read_parquet(events_path) if events_path.exists() else None
    run_record = json.loads(run_path.read_text(encoding="utf-8")) if run_path.exists() else None

    source = upsert_licence_and_source(session, entry, registry.version)

    run = SourceRun(
        source_id=source.id,
        trigger=(run_record or {}).get("trigger", "manual"),
        started_at=_to_datetime((run_record or {}).get("started_at")) or utcnow(),
        finished_at=_to_datetime((run_record or {}).get("finished_at")),
        status=(run_record or {}).get("status", "ok"),
        http_status=(run_record or {}).get("http_status"),
        bytes=(run_record or {}).get("bytes"),
        egress_class=(run_record or {}).get("egress_class", entry.egress),
        rows_seen=(run_record or {}).get("rows_seen", len(records_df)),
        rows_new=(run_record or {}).get("rows_new", 0),
        rows_changed=(run_record or {}).get("rows_changed", 0),
        rows_gone=(run_record or {}).get("rows_gone", 0),
        events_emitted=(run_record or {}).get(
            "events_emitted", len(events_df) if events_df is not None else 0
        ),
        dq_status=(run_record or {}).get("dq_status"),
        dq=(run_record or {}).get("dq"),
    )
    session.add(run)
    session.flush()

    # Proposal frames carry `lifecycle_state`; opportunity frames carry `status` + `title`
    # instead (pipeline.connectors.base PROPOSAL_COLUMNS vs OPPORTUNITY_COLUMNS) — a cheap,
    # reliable discriminator without needing the caller to pass `kind` explicitly.
    kind: Kind = "proposal" if "lifecycle_state" in records_df.columns else "opportunity"

    result = load_dataframe(session, source, kind, records_df, events_df, run=run)
    result.source_run_id = run.id
    return result
