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

Kind (2026-09-30, lane FX2): a run loads as `proposal` or `opportunity` because its connector
class says so (`Connector.kind`), never because of the columns its frame happens to carry. This
loader writes those two kinds only. Any other kind is refused with `KindRefused` before a file is
read: `document` frames (FERC eLibrary filings; the EIA-860 ownership and GHGRP context sources,
which load through `services/ingest/ownership.py` and `services/ingest/ghgrp.py` from their own
parquet) carry a constant `lifecycle_state`, and until this change the loader read that column as
"this is a proposal frame" and published every filing as an "Untitled" proposal. Nothing writes
`document` rows yet (`services/api/documents.py`), so refusal is the only correct answer here.

Removals (2026-10-10, docs/51 §2.7 item 1): a `removed` diff row says only that a record is no
longer in its source's file. It is written as a public `withdrawn` event only for a source whose
connector declares `removal_meaning = "withdrawn"` (none does today). A source whose connector
announces removals (`announce_removals`, a publication choice: ERCOT, CAISO, NYISO and NESO, owner
decision 2026-10-10) and declares the meaning `unknown` gets the public, alertable `delisted` event,
"No longer in <register>'s report (reason not stated)", which is never drafted for social, unless
the same project is still in the current frame under another key (`Connector.project_root`: NESO's
unstaged rows becoming stages, a NYISO queue position's content suffix shifting): that re-key is a
`removed_from_source` saying so, never a public departure. Every other removal (EIA-860M's,
grants.gov's) is a `removed_from_source` event with NULL `published_at`/`public_at` and the
declared meaning (`completed`, `closed`, `unknown`) in `after`, which no public or paid surface
serves. The link's `gone_at` is set in every case, and no removal changes a record's lifecycle
state (it never did).

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
  - **Entity slug collisions** (2026-10-07). A new proposal/opportunity slug is the slugified
    title plus the last six digits of its public id, which for a uuid v7 are 30 random bits: two
    records with one title ("Untitled") collided about once per 1,200 full eval-fixture loads and
    the flush raised `UNIQUE constraint failed: proposal.slug`. `services.ids.unique_slug` now
    checks every new slug against the table's (`_LoadCache.entity_slugs`) and lengthens the tail
    on a clash. Only creation assigns a slug; a reload never touches one.
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import logging
import pathlib
import re
import uuid as _uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.base import REMOVAL_MEANINGS, RemovalMeaning
from pipeline.connectors.dedupe import (
    CONTENT_SUFFIX_RE,
    content_disambiguator,
    raw_disambiguator,
    split_key,
)
from pipeline.connectors.opportunity import known_budget
from pipeline.connectors.registry import (
    GATED_REUSE,
    PUBLISHABLE_REUSE,
    RegistrationError,
    Registry,
    SourceEntry,
)
from pipeline.connectors.store import Store
from pipeline.normalize import MILESTONE_COLUMNS, milestones_from_raw
from services.db.models import (
    DELISTED_EVENT_TYPE,
    DELISTED_REASON,
    DELISTED_WORDING,
    REMOVED_FROM_SOURCE_EVENT_TYPE,
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
from services.ids import public_id, unique_slug
from services.ingest.geocode import (
    CountyGazetteer,
    default_gazetteer,
    default_substation_gazetteer,
    geocode,
)
from services.ingest.interconnection import LinkResult, link_source_points
from services.ingest.lag import record_public_at
from services.ingest.opportunity_status import close_past_deadline
from services.ingest.org_redirects import OrgRedirects
from services.ingest.vintage import NOT_STATED_VINTAGE, Vintage, from_source_urls
from services.resolve.survivorship import ROW_FIELDS, SURVIVING_FIELDS, RestatementReport
from services.resolve.survivorship import restate as restate_survivorship

if TYPE_CHECKING:  # the retirement loader imports this module; annotation only, no cycle at run time
    from services.ingest.retirements import RetirementLoadResult

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

#: The connector kinds this loader writes (`pipeline.connectors.base.Kind` also has `document`).
GENERIC_LOAD_KINDS: tuple[Kind, ...] = ("proposal", "opportunity")

#: pipeline/diff.py event_type -> docs/21 §7.3 event_type vocabulary.
#:
#: `removed` (2026-10-10, docs/51 §2.7 item 1) is not a withdrawal: it says only that the row is no
#: longer in the source's file. It maps to the non-public `removed_from_source`, and becomes a
#: public `withdrawn` only for a source whose connector declares `removal_meaning = "withdrawn"`, or
#: the public `delisted` for one that announces removals (`removal_event_type`). Before this, every
#: removal was published as `withdrawn`, so an EIA-860M unit entering operation or a grants.gov
#: notice closing reached the feed, alerts, webhooks and social drafts as a withdrawal.
DIFF_EVENT_TYPE_MAP: dict[str, str] = {
    "new": "created",
    "status_change": "status_change",
    "withdrawn": "withdrawn",
    "capacity_change": "capacity_changed",
    "cod_change": "field_changed",
    "removed": REMOVED_FROM_SOURCE_EVENT_TYPE,
}


def removal_event_type(removal_meaning: str, announce_removals: bool = False) -> str:
    """The event type a `removed` diff row is stored as: `withdrawn` only where the source declares
    that a row leaves its file because the request was withdrawn; `delisted` where the connector
    announces removals and the meaning is `unknown` (a source that states a meaning, such as
    grants.gov's `closed`, is never announced as "reason not stated"); else `removed_from_source`."""
    if removal_meaning == "withdrawn":
        return "withdrawn"
    if announce_removals and removal_meaning == "unknown":
        return DELISTED_EVENT_TYPE
    return REMOVED_FROM_SOURCE_EVENT_TYPE


def delisted_sentence(register_name: str) -> str:
    """How a `delisted` event reads: "No longer in ERCOT's report (reason not stated)"."""
    return DELISTED_WORDING.format(register=register_name)


class GateRefused(Exception):
    """The source's registry reuse class is `restricted` or `unknown` (docs/21 §8, CLAUDE.md)."""


class KindRefused(Exception):
    """The source's connector declares a kind this loader does not write (module docstring,
    "Kind"), or no connector declares one and the caller did not name it."""


def connector_kind(registry: Registry, source_id: str) -> str | None:
    """The `kind` the source's connector class declares, or None when the source has no connector
    class (a manifest-only entry, a private aggregator, a test fixture)."""
    try:
        cls = registry.connector_class(source_id)
    except RegistrationError:
        return None
    kind = getattr(cls, "kind", None)
    return str(kind) if kind is not None else None


def connector_removal_meaning(registry: Registry, source_id: str) -> RemovalMeaning:
    """What a `removed` diff row means at `source_id`, as its connector class declares it
    (`Connector.removal_meaning`), read the way `connector_kind` reads `kind`. `unknown` when the
    source has no connector class or declares a value outside `REMOVAL_MEANINGS`; the second is
    logged. Failing to `unknown` fails closed: the removal is stored, never published."""
    try:
        cls = registry.connector_class(source_id)
    except RegistrationError:
        return "unknown"
    declared = getattr(cls, "removal_meaning", "unknown")
    for meaning in REMOVAL_MEANINGS:
        if declared == meaning:
            return meaning
    log.warning(
        "%s: connector removal_meaning=%r is not one of %s; read as 'unknown'",
        source_id,
        declared,
        REMOVAL_MEANINGS,
    )
    return "unknown"


def connector_announces_removals(registry: Registry, source_id: str) -> bool:
    """Whether `source_id`'s connector class announces removals (`Connector.announce_removals`).
    Only a literal True counts; no connector class, or any other value, is False (fails closed: the
    removal is stored, never published)."""
    try:
        cls = registry.connector_class(source_id)
    except RegistrationError:
        return False
    return getattr(cls, "announce_removals", False) is True


def _same_key(record_key: str) -> str:
    """`Connector.project_root`'s default: every key is its own project."""
    return record_key


def connector_project_root(registry: Registry, source_id: str) -> Callable[[str], str]:
    """`source_id`'s `Connector.project_root`, or the identity when the source has no connector
    class (each key its own project, so nothing is held back as a re-key)."""
    try:
        cls = registry.connector_class(source_id)
    except RegistrationError:
        return _same_key
    hook = getattr(cls, "project_root", None)
    return hook if callable(hook) else _same_key


def connector_register_name(registry: Registry, source_id: str) -> str | None:
    """The register's display name its connector declares (`Connector.register_name`), or None."""
    try:
        cls = registry.connector_class(source_id)
    except RegistrationError:
        return None
    name = getattr(cls, "register_name", None)
    if not isinstance(name, str):
        return None
    return name.strip() or None


def generic_load_kind(registry: Registry, source_id: str, requested: Kind | None = None) -> Kind:
    """The kind `load_from_files` loads `source_id` as, or `KindRefused`.

    The connector class decides. `requested` is accepted only where it agrees with that class, or
    where no connector class exists (a fixture source in a test): it can name a kind, never
    override one. Every refusal is logged with its reason."""
    declared = connector_kind(registry, source_id)
    reason: str | None = None
    kind: str | None = declared if declared is not None else requested
    if declared is not None and requested is not None and requested != declared:
        reason = f"{source_id}: its connector declares kind={declared!r}, the caller asked for {requested!r}"
    elif kind is None:
        reason = f"{source_id}: no connector class declares a kind and none was given"
    elif kind not in GENERIC_LOAD_KINDS:
        reason = (
            f"{source_id}: connector kind={kind!r}; the generic loader writes only "
            f"{'/'.join(GENERIC_LOAD_KINDS)} rows, never a {kind} frame as proposals"
        )
    if reason is not None:
        log.warning("load refused: %s", reason, extra={"source_id": source_id, "kind": declared})
        raise KindRefused(reason)
    if kind == "proposal":
        return "proposal"
    return "opportunity"


#: Sources whose run output is not proposals or opportunities but has a loader of its own, which
#: `load_from_files` hands the run to (same arguments, same gate first). The scheduler queues their
#: loads like any other (`kind_refusal` answers None), and the generic kind rule still refuses to
#: load their frames as proposals (`generic_load_kind`). `module:function`, imported on use.
SPECIALISED_LOADERS: dict[str, str] = {
    # Lane R1 (2026-10-06): generator retirements -> power_plant assets and their events.
    "us.eia.860m.retirements": "services.ingest.retirements:load_retirement_run",
    # 2026-10-07: ERCOT's large-load catalogue watch. Its rows are report products, not projects,
    # so they never become proposals (services/ingest/large_load_watch.py).
    "us.iso.ercot.large_load_queue": "services.ingest.large_load_watch:load_catalogue_watch_run",
}


def specialised_loader(source_id: str) -> Any | None:
    """The loader function `SPECIALISED_LOADERS` names for `source_id`, or None."""
    target = SPECIALISED_LOADERS.get(source_id)
    if target is None:
        return None
    module_name, _, function_name = target.partition(":")
    import importlib

    return getattr(importlib.import_module(module_name), function_name)


def kind_refusal(registry: Registry, source_id: str) -> str | None:
    """Why `load_from_files` would refuse `source_id` by kind, or None when it loads it. The
    scheduler asks this before it queues a load (`infra/scheduler/app.py::_defer_load`)."""
    if source_id in SPECIALISED_LOADERS:
        return None
    declared = connector_kind(registry, source_id)
    if declared is None:
        return f"{source_id}: no connector class declares a kind"
    if declared not in GENERIC_LOAD_KINDS:
        return f"{source_id}: connector kind={declared!r} is not loaded by the generic loader"
    return None


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
    #: Of `events_created`, the `removed` diff rows stored as non-public `removed_from_source`.
    removals_unpublished: int = 0
    #: Of `events_created`, the `removed` diff rows published as `delisted`.
    removals_announced: int = 0
    #: Of `removals_unpublished`, the removals not announced because the same project is still in
    #: the frame under another key (`Connector.project_root`).
    removals_rekeyed: int = 0
    locations_created: int = 0
    locations_exact_promoted: int = 0
    organizations_created: int = 0
    #: Updated records whose served values this load changed: the ones whose `last_changed` moved.
    records_changed: int = 0
    #: Opportunities this load left `closed` because their deadline had passed when it ran,
    #: though their frame said `open` (`services/ingest/opportunity_status.py`).
    deadline_closed: int = 0
    warnings: list[str] = field(default_factory=list)
    #: The grid interconnection pass over this load's proposal links (`None` for opportunities).
    interconnection: LinkResult | None = None
    #: What this load wrote, `proposal` or `opportunity`: the connector's declared kind.
    kind: str | None = None
    #: Field survivorship over the touched proposals that hold more than one source link
    #: (`services/resolve/survivorship.py`; docs/22 §23). No events: a member's news is its own
    #: source's diff event.
    survivorship: RestatementReport | None = None


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


def _assert_loadable(entry: SourceEntry) -> None:
    """Every refusal the manifest entry alone decides, before any row is written: a gated reuse
    class, `publication: none`, a reuse class outside the posture's publishable set, or a
    `publication` value outside `PUBLICATION_VALUES`."""
    _assert_not_gated(entry)
    if entry.publication == "none":
        raise GateRefused(f"{entry.id}: publication=none publishes nothing (docs/21 §8)")
    # Affirmative membership in the posture's publishable set (`services/posture.py`), not
    # mere absence from the gated set: an unrecognised value fails closed here.
    if entry.reuse not in PUBLISHABLE_REUSE:
        raise GateRefused(f"{entry.id}: reuse={entry.reuse!r} is not publishable")
    if entry.publication is not None and entry.publication not in PUBLICATION_VALUES:
        raise GateRefused(f"{entry.id}: publication={entry.publication!r} not in {PUBLICATION_VALUES}")


def load_refusal(entry: SourceEntry) -> str | None:
    """Why this loader would refuse every row of `entry`, or None when it loads them. The one
    definition of "a source the store loads" for callers that must not see what it would refuse,
    such as the scheduler's resolution step (`infra/scheduler/jobs.py::_latest_proposal_frames`,
    docs/25 §3.9)."""
    try:
        _assert_loadable(entry)
    except GateRefused as exc:
        return str(exc)
    return None


def upsert_licence_and_source(session: Session, entry: SourceEntry, manifest_version: str) -> Source:
    """Mirror one `data/sources.yaml` entry into `licence` + `source` (docs/21 §4.1).

    Both gate checks must hold: the caller re-verifies `entry.reuse` before calling this (see
    `_assert_not_gated`), and this function refuses a second time on the licence row it is about
    to write, so a bug in the caller cannot silently widen the gate.
    """
    _assert_loadable(entry)
    licence_id = entry.licence_id

    licence = session.get(Licence, licence_id)
    now = utcnow()
    derived_only = _is_derived_only_override(entry)
    if licence is None:
        licence = Licence(
            id=licence_id,
            name=f"{entry.name} terms of use",
            reuse_class=entry.reuse,
            # `noncommercial` (docs/26): attribution and a link back are required, exactly as for
            # `attribution` — CC BY-NC's own terms — and the row is publishable only while the
            # posture admits the class, which is the registry gate above, not this row.
            attribution_required=entry.reuse in ("attribution", "noncommercial"),
            # The manifest's credit line, verbatim (`SourceEntry.credit_text`: `attribution` plus
            # any `changes_statement`, else `Source: <operator>` for an attribution class).
            # 2026-10-06, L-2: NESO's licence ends automatically unless the exact statement
            # "Supported by National Energy SO Open Data" is shown, and the generic line was not it.
            attribution_text=entry.credit_text,
            url=entry.licence_url or None,
            requires_link_back=entry.reuse in ("attribution", "noncommercial"),
            allows_derived_publication=True,
            # docs/21 §8: `open` is always raw-ok; `attribution` is raw-ok too unless this
            # source's own terms are recorded as a derived-only override (module docstring above)
            # -- never hardcoded True regardless of reuse class (was services/README.md open
            # decision #11).
            allows_raw_publication=not derived_only,
            # A noncommercial grant never leaves over the API or in a bulk export: both are the
            # paid shapes (docs/26 precondition iii), so the flags are written false at load and
            # `allows_commercial_use` is false by the class's definition (the
            # `noncommercial_no_commercial_use` CHECK on `licence` refuses otherwise). For
            # `attribution` the commercial flag stays false as before: the manifest class covers
            # `attribution-restricted` register rows too, so "not verified" is the honest value.
            allows_api_redistribution=entry.reuse != "noncommercial",
            allows_bulk_export=entry.reuse != "noncommercial",
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
    else:
        refresh_licence_credit(licence, entry)
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
            # to hold. Since 2026-09-25 the set is the posture's (`services/posture.py`), which
            # is what lets a `noncommercial` source load straight to public under the
            # `noncommercial` posture and never under `commercial`.
            publish_state="public" if entry.reuse in PUBLISHABLE_REUSE else "ingest_only",
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


def refresh_licence_credit(licence: Licence, entry: SourceEntry) -> bool:
    """Bring a stored licence row's credit metadata (`attribution_text`, `url`) to what the
    manifest says now. These say how the terms are honoured, not what the terms are (a change to
    the terms changes `licence_id` itself), so they follow the manifest on every load instead of
    keeping whatever the first load wrote. A manifest that states neither leaves the row alone.
    Returns whether anything changed."""
    changed = False
    credit = entry.credit_text
    if credit is not None and licence.attribution_text != credit:
        licence.attribution_text = credit
        changed = True
    if entry.licence_url and licence.url != entry.licence_url:
        licence.url = entry.licence_url
        changed = True
    return changed


def refresh_all_licence_credits(session: Session, registry: Registry | None = None) -> list[str]:
    """`refresh_licence_credit` for every stored source the manifest lists, without loading any
    data: the one-off that puts a corrected credit on every surface at once (run after a manifest
    credit change; `python -m services.ingest.loader --refresh-credits`). Returns the licence ids
    changed."""
    registry = registry or Registry()
    changed: list[str] = []
    for source in session.scalars(select(Source)).all():
        entry = registry.sources.get(source.id)
        if entry is not None and refresh_licence_credit(source.licence, entry):
            changed.append(source.licence_id)
    session.flush()
    return changed


def set_source_vintage(source: Source, vintage: Vintage) -> None:
    """Record the release this source states, as resolved from the artefact just loaded.

    Written on every load, including when the answer is `not_stated`: "we looked and the source
    publishes no release label" is an answer the surfaces render, and leaving the column NULL
    would make it indistinguishable from "never examined". Deliberately has no access to
    `retrieved_at` — the whole point of the column is that a fetch date can never leak into it
    (`services/ingest/vintage.py`)."""
    source.vintage = vintage.value
    source.vintage_basis = vintage.basis


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
    #: organisation regardless of `merged_into_id` — matches the old exact-match query's scope —
    #: but live rows first and a merged row's spelling mapped to its survivor
    #: (`services/ingest/org_redirects.py`, 2026-09-27): a re-run after a merge must not sponsor
    #: new proposals by the redirect.
    org_by_exact: dict[str, Organization]
    #: `_org_punct_key(name_canonical)` -> `Organization`: live organisations first, then each
    #: merged row's spelling mapped to its survivor where no live row holds the key (before
    #: 2026-09-27 merged rows were skipped, so a punctuation variant of an absorbed spelling
    #: created a second copy of the organisation the merge had retired).
    org_by_punct: dict[str, Organization]
    #: `(organization_id, alias_normalised)` pairs already on file, any organisation.
    alias_keys: set[tuple[_uuid.UUID, str]]
    #: every `organization.slug` already taken, any organisation — the old collision re-check's scope.
    existing_slugs: set[str]
    #: every `proposal.slug` / `opportunity.slug` (whichever `entity_cls` this call loads) already
    #: taken, merged rows included, plus each slug this call assigns: the `taken` set
    #: `services.ids.unique_slug` checks a new entity's slug against (2026-10-07: two "Untitled"
    #: proposals drew the same 30-bit random suffix and the flush raised `UNIQUE constraint
    #: failed: proposal.slug`).
    entity_slugs: set[str]
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
    redirects = OrgRedirects(session)
    merged: list[Organization] = []
    for org in session.scalars(select(Organization)):
        existing_slugs.add(org.slug)
        if org.merged_into_id is None:
            org_by_exact.setdefault(org.name_normalised, org)
            org_by_punct.setdefault(_org_punct_key(org.name_canonical), org)
        else:
            merged.append(org)
    # Live rows first: a redirect's spelling reaches its survivor only where no live row holds it.
    for org in merged:
        survivor = redirects.terminal(org)
        org_by_exact.setdefault(org.name_normalised, survivor)
        org_by_punct.setdefault(_org_punct_key(org.name_canonical), survivor)

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
        entity_slugs=set(session.scalars(select(entity_cls.slug))),
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
    # Defence in depth: two letter-distinct names should never coincidentally collide once
    # `_org_punct_key` above has already ruled out a punctuation-only match, but a checked
    # suffix here means a bug in that reasoning fails safe (no row, no crash) rather than
    # raising `UNIQUE constraint failed: organization.slug` at ingestion.
    slug = unique_slug(raw_name, org_public_id, cache.existing_slugs, bare_first=True)
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


#: The NESO TEC register's transmission-owner column (NGET, SPT, SHET, OFTO): the region the GB
#: settlement fallback requires (`services.ingest.geocode.geocode`, `region`).
GB_REGION_FIELD = "HOST TO"


def _gb_region(raw_payload: Mapping[str, Any] | None) -> str | None:
    value = (raw_payload or {}).get(GB_REGION_FIELD)
    text = str(value).strip() if value is not None else ""
    return text or None


def _gb_geocoder(county: str | None) -> str:
    """Which GB tier placed a row `geocode` placed: the substation list first, as `geocode` tries
    it, else the settlement fallback (docs/21 §3.7 `geocoder`)."""
    on_substation_list = default_substation_gazetteer().substation_point(county) is not None
    return "gb_substation" if on_substation_list else "gb_settlement"


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
        # proxy, stamped `gb_substation` so the API and the map can say so (docs/21 §3.7). A miss
        # falls back to the settlement tier, which places nothing without the transmission owner
        # the row names (`HOST TO`): passing it is what turns that tier on (audit 2026-10-07
        # DATA-13; before, every NESO row the substation list missed stayed `unknown`). A
        # settlement hit is stamped `gb_settlement`.
        region = _gb_region(raw_payload) if country_code == "GB" else None
        point, precision = geocode(state, county, gaz=gaz, country=country_code, region=region)
        geocoder = _gb_geocoder(county) if country_code == "GB" and point is not None else None
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


#: `proposal.identifiers` key for why a selecting connector kept a row, as `{source_id: basis}`
#: (lane H6, 2026-09-29). The data-centre connectors (`us.va.deq.data_center_air_sites`,
#: `us.epa.echo.icis_air`) put a `select_basis` token on every row's raw payload (docs/25 §3.3,
#: §3.7); `raw` is admin-only, so without this the public page cannot say why a site counts as a
#: data centre, and one basis (`naics_518210`) is measurably weaker than the others. Keyed by
#: source because one merged proposal can carry both sources, each with its own reason.
SELECT_BASIS_KEY = "select_basis"


def _select_basis(row: Mapping[str, Any]) -> str | None:
    """The row's `select_basis` token from its raw payload, or `None`. The substring test skips
    the JSON parse for every source that never writes one (all but the two above)."""
    raw_value = _row_get(row, "raw")
    if isinstance(raw_value, str) and SELECT_BASIS_KEY not in raw_value:
        return None
    basis = _parse_raw(raw_value).get(SELECT_BASIS_KEY)
    return str(basis) if basis else None


def _keep_other_sources_basis(
    stored: Mapping[str, Any] | None, incoming: dict[str, Any], source_id: str
) -> dict[str, Any]:
    """`incoming` identifiers, plus the stored `select_basis` entries of *other* sources.

    An update replaces `identifiers` with what this source's row says. On a proposal that two
    sources feed (a Virginia DEQ site merged with its ICIS-Air record), that would erase the other
    source's basis on every load and restore it on the next. This source's own entry is always
    the incoming one, so a basis that changes, or disappears, at its own source is followed."""
    others = {
        sid: basis
        for sid, basis in (((stored or {}).get(SELECT_BASIS_KEY)) or {}).items()
        if sid != source_id
    }
    if not others:
        return incoming
    mine = incoming.get(SELECT_BASIS_KEY) or {}
    return {**incoming, SELECT_BASIS_KEY: {**others, **mine}}


def _proposal_fields_from_row(row: Mapping[str, Any], source: Source) -> dict[str, Any]:
    queue_id = _row_get(row, "queue_id")
    identifiers: dict[str, Any] = {}
    if queue_id:
        identifiers["queue_ids"] = [{"iso": _row_get(row, "iso") or "", "id": str(queue_id)}]
    if _row_get(row, "eia_plant_id"):
        identifiers["eia_plant_id"] = str(_row_get(row, "eia_plant_id"))
    if _row_get(row, "eia_generator_id"):
        identifiers["eia_generator_id"] = str(_row_get(row, "eia_generator_id"))
    basis = _select_basis(row)
    if basis:
        identifiers[SELECT_BASIS_KEY] = {source.id: basis}

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


def _proposal_milestones_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """The milestones a proposal row states (`pipeline.normalize.MILESTONE_COLUMNS`: queue date,
    study phase, IA date, withdrawn date, actual COD), non-null only, dates as `date`. A frame
    written before 2026-10-07 lacks the columns; its rows are read from their `raw` payload by the
    same table (`milestones_from_raw`), so a reload of a stored frame also carries them."""
    out: dict[str, Any] = {}
    for name in MILESTONE_COLUMNS:
        value = _row_get(row, name)
        if value is None:
            continue
        if name == "study_phase":
            text = str(value).strip()
            if text:
                out[name] = text
            continue
        date = _to_date(value)
        if date is not None:
            out[name] = date
    if set(out) - {"queue_date"}:
        return out
    from_raw = milestones_from_raw(str(_row_get(row, "source_id") or ""), _parse_raw(_row_get(row, "raw")))
    return {**from_raw, **out}


def _milestones_provenance(
    milestones: Mapping[str, Any], source: Source, retrieved_at: dt.datetime
) -> dict[str, Any]:
    """`field_provenance[<milestone>]` for a record one source states: the usual provenance entry plus
    the milestone's `value` (the proposal table has no column for them). Survivorship rewrites them
    for a record with several links (`services.resolve.survivorship.milestones`). A reader serves a
    value only when its `source_id` is readable, as for any other field."""
    return {
        name: {
            "value": _jsonable(value),
            "source_id": source.id,
            "licence_id": source.licence_id,
            "retrieved_at": retrieved_at.isoformat(),
            "rule": "stated_by_member",
        }
        for name, value in milestones.items()
    }


def _link_normalised(
    fields: Mapping[str, Any], milestones: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """A link's own `normalised` row: the record fields this source states, plus (proposals,
    2026-10-07) the identifiers it contributes, so field survivorship can union every member's
    queue ids and EIA ids however many sources a record holds (docs/22 §23), and (2026-10-07,
    lane L10) the milestones it states (`_proposal_milestones_from_row`). `select_basis` stays
    on the record (`_keep_other_sources_basis`)."""
    out: dict[str, Any] = {k: v for k, v in fields.items() if not isinstance(v, dict)}
    for name, value in (milestones or {}).items():
        out.setdefault(name, value)
    identifiers = fields.get("identifiers")
    if isinstance(identifiers, dict):
        own = {k: v for k, v in identifiers.items() if k != SELECT_BASIS_KEY}
        if own:
            out["identifiers"] = own
    normalised: dict[str, Any] = _jsonable(out)
    return normalised


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
        # Also here, not only in the connectors: parquet snapshots written before lane I3 still
        # carry TED's 0 / -1 placeholders, and every connector's rows enter the store through here.
        "budget_amount": known_budget(_to_float(_row_get(row, "budget_amount"))),
        "budget_currency": _row_get(row, "budget_currency"),
        "open_at": _to_date(_row_get(row, "open_at")),
        "due_at": _to_datetime(_row_get(row, "due_at")),
        "status": _row_get(row, "status") or "unknown",
        "status_raw": _row_get(row, "status_raw"),
        "identifiers": identifiers,
    }


def re_split(value: str) -> list[str]:
    """A stored list from a frame's string cell. `|` is the separator the opportunity connectors
    write (`pipeline.connectors.opportunity.technologies_str`); `,` and `;` are kept for older
    frames. Before 2026-10-06 `|` was not split, so 72 stored rows held one joined element such
    as `solar_pv|nuclear` that no technology filter matched (audit 2026-09-30, frontend F2)."""
    return [v.strip() for v in value.replace(";", ",").replace("|", ",").split(",")]


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


def _record_key(source: Source, record_id: str) -> str:
    """A connector `record_id` without its `<source_id>:` prefix: the stored link key."""
    prefix = f"{source.id}:"
    return record_id[len(prefix) :] if record_id.startswith(prefix) else record_id


def _link_for_event_record_id(cache: _LoadCache, source: Source, record_id: str) -> Any | None:
    """A stored link for an event's `record_id` when the record is not in this frame (a
    `removed` event): the connector key with the source prefix stripped is the stored key."""
    return cache.links.get(_record_key(source, record_id))


def _short_hash(value: Any) -> str:
    return hashlib.sha1(  # noqa: S324 — identity key, not security
        json.dumps(value, default=str, sort_keys=True).encode()
    ).hexdigest()[:12]


@dataclass(frozen=True)
class _LoadContext:
    """The per-load constants and shared state every step takes, in place of its own copy of
    `source`/`kind`/`run`/`cache`/etc; the mutable fields still mutate in place as before this
    extraction. `record_id_to_internal` is how `_upsert_records` hands `_load_events` each row's
    subject."""

    now: dt.datetime
    result: LoadResult
    entity_cls: type[Any]
    link_cls: type[Any]
    fk_name: str
    cache: _LoadCache
    record_id_to_internal: dict[str, _uuid.UUID]
    source: Source
    kind: Kind
    run: SourceRun | None
    #: What a `removed` diff row means at this source (`connector_removal_meaning`).
    removal_meaning: RemovalMeaning = "unknown"
    #: Whether this source's removals are announced as `delisted` (`connector_announces_removals`).
    announce_removals: bool = False
    #: The register's display name a `delisted` event names (`connector_register_name`, else the
    #: source's operator, else its name).
    register_name: str = ""
    #: The project a record key belongs to (`connector_project_root`), and the projects this frame
    #: lists: a removal whose project is among them is a re-key, never announced.
    project_root: Callable[[str], str] = _same_key
    current_project_roots: frozenset[str] = frozenset()
    #: Per updated record, its `last_changed` and served values before this load touched it
    #: (`_stamp_last_changed`); the first sight in a load wins.
    served_before: dict[_uuid.UUID, tuple[dt.datetime | None, dict[str, Any]]] = field(default_factory=dict)


def _served_keys(kind: Kind, fields: Mapping[str, Any]) -> tuple[str, ...]:
    """The record values a reader is served that a load can move: this source's fields and, for a
    proposal, everything field survivorship writes (docs/22 §23)."""
    if kind == "proposal":
        return tuple(dict.fromkeys((*fields, *SURVIVING_FIELDS, *ROW_FIELDS)))
    return tuple(fields)


def _served_norm(value: Any) -> Any:
    """A served value in a form that compares equal however the store handed it back: a SQLite
    `Numeric` reads back as `Decimal`, a timestamp may come back naive (UTC), JSON key order is
    not significant."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, decimal.Decimal)):
        return round(float(value), 6)
    if isinstance(value, dt.datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
        return aware.astimezone(dt.UTC).isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, _uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _served_norm(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_served_norm(v) for v in value]
    return value


def _served_values(entity: Any, keys: Iterable[str]) -> dict[str, Any]:
    return {k: _served_norm(getattr(entity, k, None)) for k in keys}


def _stamp_last_changed(ctx: _LoadContext) -> None:
    """Move `last_changed` (docs/21 §3.1: the latest change to a served field) on the records whose
    served values this load changed, and only those (audit 2026-10-07 DATA-5: every load stamped
    every row it rewrote, 2,311 of 2,311 on an EIA-860M reload that changed 4). A record whose
    values came back the same keeps its stamp, including one where field survivorship moved
    `last_changed` only because this load wrote a member's own value before it recomputed the
    record's (`services/resolve/survivorship.py`)."""
    for entity_id, (prior, before) in ctx.served_before.items():
        entity = ctx.cache.entities.get(entity_id)
        if entity is None:
            continue
        if _served_values(entity, before) != before:
            entity.last_changed = ctx.now
            ctx.result.records_changed += 1
        elif prior is not None:
            entity.last_changed = prior


def _prepare_load_context(
    session: Session,
    source: Source,
    kind: Kind,
    records_df: pd.DataFrame,
    run: SourceRun | None,
    removal_meaning: RemovalMeaning = "unknown",
    announce_removals: bool = False,
    register_name: str | None = None,
    project_root: Callable[[str], str] | None = None,
) -> _LoadContext:
    """`load_dataframe`'s setup step (module phase map blocks 01-10)."""
    now = utcnow()
    result = LoadResult(source_run_id=run.id if run else _uuid.UUID(int=0))
    record_id_to_internal: dict[str, _uuid.UUID] = {}

    entity_cls: type[Any] = Proposal if kind == "proposal" else Opportunity
    link_cls: type[Any] = ProposalSource if kind == "proposal" else OpportunitySource
    fk_name = "proposal_id" if kind == "proposal" else "opportunity_id"

    # The release this run's artefact states, beside the fetch date the link rows already carry
    # (`services/ingest/vintage.py`). Resolved from the URLs the connector wrote onto the frame,
    # so it is the source's own statement rather than anything inferred from when we ran.
    set_source_vintage(
        source,
        from_source_urls(records_df["source_url"])
        if "source_url" in records_df.columns
        else NOT_STATED_VINTAGE,
    )
    cache = _build_load_cache(session, source, entity_cls, link_cls, fk_name)
    root_of = project_root or _same_key
    current_roots = (
        frozenset(root_of(_record_key(source, str(rid))) for rid in records_df["record_id"])
        if announce_removals and "record_id" in records_df.columns
        else frozenset()
    )
    return _LoadContext(
        now=now,
        result=result,
        entity_cls=entity_cls,
        link_cls=link_cls,
        fk_name=fk_name,
        cache=cache,
        record_id_to_internal=record_id_to_internal,
        source=source,
        kind=kind,
        run=run,
        removal_meaning=removal_meaning,
        announce_removals=announce_removals,
        register_name=(register_name or "").strip() or source.operator or source.name or source.id,
        project_root=root_of,
        current_project_roots=current_roots,
    )


def _index_records(records_df: pd.DataFrame) -> tuple[list[dict[str, Any]], set[str]]:
    """Module phase map blocks 11-16: the frame as plain dicts, plus every `source_record_id`
    more than one `record_id` claims this call (module docstring, "Intra-run id reuse")."""
    records = records_df.to_dict("records") if len(records_df) else []
    distinct_record_ids: dict[str, set[str]] = {}
    for row in records:
        distinct_record_ids.setdefault(str(_row_get(row, "source_record_id")), set()).add(
            str(_row_get(row, "record_id"))
        )
    dup_naturals = {key for key, ids in distinct_record_ids.items() if len(ids) > 1}
    return records, dup_naturals


@dataclass
class _UpsertState:
    """One `_upsert_records` pass's mutable state, threaded into `_claim_source_record_key` and
    `_create_entity_and_link` instead of four separate mutable-container parameters each."""

    key_by_record_id: dict[str, str] = field(default_factory=dict)
    claimed: dict[str, str] = field(default_factory=dict)
    warned_reuse: set[str] = field(default_factory=set)
    #: New `ProposalSource`/`OpportunitySource` rows, held back from `session.add` until the next
    #: `_flush_pending` call so their entity is guaranteed already flushed first (see that
    #: function's docstring for why order matters here).
    pending_links: list[Any] = field(default_factory=list)


def _row_fields(ctx: _LoadContext, row: Mapping[str, Any]) -> dict[str, Any]:
    """The row's `proposal`/`opportunity` fields -- a pure function of `row` and `ctx.source`, so
    each per-row step calls it off `row` instead of taking the dict as its own parameter."""
    if ctx.kind == "proposal":
        return _proposal_fields_from_row(row, ctx.source)
    return _opportunity_fields_from_row(row)


def _claim_source_record_key(
    ctx: _LoadContext,
    state: _UpsertState,
    row: Mapping[str, Any],
    record_id: str,
    dup_naturals: set[str],
) -> tuple[str, Any | None, dict[str, Any]]:
    """One row's stored `source_record_id`, its link if any, and its fields -- `fields` is
    computed exactly once per row here, on every path, and handed to the caller instead of being
    recomputed in the update/create branch below (module docstring, "Intra-run id reuse"); warns
    once per natural key on true intra-run reuse."""
    fields = _row_fields(ctx, row)
    raw_source_record_id = str(_row_get(row, "source_record_id"))
    if record_id in state.key_by_record_id:
        source_record_id = state.key_by_record_id[record_id]
        existing_link = ctx.cache.links.get(source_record_id)
    else:
        in_dup_group = raw_source_record_id in dup_naturals
        wanted = _wanted_key(ctx.source, raw_source_record_id, record_id, row, in_dup_group=in_dup_group)
        wanted = _unclaimed_key(wanted, raw_source_record_id, str(_row_get(row, "raw") or ""), state.claimed)
        existing_link = _match_link(ctx.cache, wanted, raw_source_record_id, fields, ctx.kind, state.claimed)
        source_record_id = str(existing_link.source_record_id) if existing_link is not None else wanted
        state.key_by_record_id[record_id] = source_record_id
        state.claimed[source_record_id] = record_id
        if in_dup_group and raw_source_record_id in state.warned_reuse:
            # A different connector `record_id` sharing this natural key inside the same
            # run/dataframe -- never merged with its sibling(s); every member is stored
            # under its content key (module docstring, docs/22 §5/§7.1).
            warning = (
                f"{ctx.source.id}: source_record_id {raw_source_record_id!r} reused by a "
                f"distinct record ({record_id!r}) within this run; stored as "
                f"{source_record_id!r} rather than merged into its sibling "
                "(docs/22 §5, §7.1)"
            )
            ctx.result.warnings.append(warning)
            _record_dq_warning(
                ctx.run,
                check="duplicate_source_record_id_same_run",
                detail=warning,
                data={
                    "source_record_id": raw_source_record_id,
                    "record_id": record_id,
                    "stored_as": source_record_id,
                },
            )
        state.warned_reuse.add(raw_source_record_id)
    return source_record_id, existing_link, fields


def _update_existing_entity(
    ctx: _LoadContext, existing_link: Any, row: Mapping[str, Any], fields: dict[str, Any]
) -> None:
    """The already-stored-link branch: re-stamp field provenance per changed field (module
    docstring, "Field provenance"), refresh the link, clear `gone_at`.

    A field named in the entity's `overrides` is a human decision (docs/21 §6.4: "the normaliser
    and the enricher skip overridden fields until a user clears the override") and is neither
    written nor re-stamped: an operator's correction or privacy redaction survives every later
    load (2026-10-06, L-1 of the 2026-09-30 legal audit; docs/13 §5.4 rule 6). The source's own
    view still lands on its link row (`normalised`, `raw`), so clearing the override and
    reloading restores the source's value."""
    raw_payload = _parse_raw(_row_get(row, "raw"))
    retrieved_at = _to_datetime(_row_get(row, "retrieved_at")) or ctx.now
    entity = ctx.cache.entities.get(getattr(existing_link, ctx.fk_name))
    if entity is None:
        raise RuntimeError(
            f"proposal_source/opportunity_source row {existing_link.id} points at a "
            "missing entity — this is a store consistency bug, not a data error"
        )
    if ctx.kind == "proposal" and "identifiers" in fields:
        fields = {
            **fields,
            "identifiers": _keep_other_sources_basis(
                entity.identifiers, fields["identifiers"], ctx.source.id
            ),
        }
    if entity.id not in ctx.served_before:
        keys = _served_keys(ctx.kind, fields)
        ctx.served_before[entity.id] = (entity.last_changed, _served_values(entity, keys))
    provenance = dict(entity.field_provenance or {})
    pinned = set(entity.overrides or {})
    for k, v in fields.items():
        if k in pinned:
            continue
        if v is not None and (k not in provenance or getattr(entity, k, None) != v):
            provenance[k] = {
                "source_id": ctx.source.id,
                "licence_id": ctx.source.licence_id,
                "retrieved_at": retrieved_at.isoformat(),
            }
        setattr(entity, k, v)
    milestones = _proposal_milestones_from_row(row) if ctx.kind == "proposal" else {}
    if ctx.kind == "proposal":
        for name in MILESTONE_COLUMNS:
            provenance.pop(name, None)
        provenance.update(_milestones_provenance(milestones, ctx.source, retrieved_at))
    entity.field_provenance = provenance  # reassigned so the JSON column is marked dirty
    # `last_changed` is decided once the whole load (survivorship included) has run: it moves only
    # when a served value differs from what the record served before this load (`_stamp_last_changed`).
    existing_link.raw = raw_payload
    existing_link.normalised = _link_normalised(fields, milestones)
    existing_link.status_raw = fields.get("status_raw")
    existing_link.last_seen = retrieved_at
    existing_link.retrieved_at = retrieved_at
    existing_link.gone_at = None  # seen again: no longer gone from the register
    if ctx.kind == "proposal":
        ctx.result.proposals_updated += 1
    else:
        ctx.result.opportunities_updated += 1


def _create_entity_and_link(
    session: Session,
    ctx: _LoadContext,
    state: _UpsertState,
    row: Mapping[str, Any],
    source_record_id: str,
    fields: dict[str, Any],
) -> Any:
    """The no-stored-link branch: a brand-new entity (with sponsor/location, for a `proposal`)
    and its link, held in `state.pending_links`. Returns the new link."""
    raw_payload = _parse_raw(_row_get(row, "raw"))
    retrieved_at = _to_datetime(_row_get(row, "retrieved_at")) or ctx.now
    source, kind, cache, result = ctx.source, ctx.kind, ctx.cache, ctx.result

    published_at = ctx.now
    # Paywall by shape, not by time (owner, 2026-09-19): a record is visible to a
    # free reader the moment it is published. `public_at == published_at` here, and
    # since 2026-09-21 the event loop below says exactly the same thing -- there is
    # no delayed shape left at all (`services/ingest/lag.py`).
    public_at = record_public_at(published_at)
    entity_id = new_uuid()
    entity_public_id = public_id("prop" if kind == "proposal" else "opp", entity_id)
    title = fields.get("name_canonical") or fields.get("title") or "record"
    slug = unique_slug(title, entity_public_id, cache.entity_slugs)
    cache.entity_slugs.add(slug)
    entity = ctx.entity_cls(
        id=entity_id,
        public_id=entity_public_id,
        slug=slug,
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
    milestones = _proposal_milestones_from_row(row) if kind == "proposal" else {}
    if milestones:
        entity.field_provenance.update(_milestones_provenance(milestones, source, retrieved_at))
    session.add(entity)
    cache.entities[entity.id] = entity
    existing_link = ctx.link_cls(
        **{ctx.fk_name: entity.id},
        source_id=source.id,
        source_record_id=source_record_id,
        source_url=str(_row_get(row, "source_url") or source.url),
        retrieved_at=retrieved_at,
        licence_id=source.licence_id,
        raw=raw_payload,
        normalised=_link_normalised(fields, milestones),
        status_raw=fields.get("status_raw"),
        first_seen=retrieved_at,
        last_seen=retrieved_at,
        link_method="deterministic_key",
        link_confidence=1.0,
    )
    state.pending_links.append(existing_link)
    cache.links[source_record_id] = existing_link
    cache.siblings.setdefault(split_key(source_record_id)[0], []).append(existing_link)
    cache.links_by_entity[entity.id] = existing_link
    if kind == "proposal":
        result.proposals_created += 1
    else:
        result.opportunities_created += 1
    return existing_link


def _upsert_records(
    session: Session,
    ctx: _LoadContext,
    records: list[dict[str, Any]],
    dup_naturals: set[str],
    batch_size: int,
) -> None:
    """`load_dataframe`'s upsert step (module phase map block 17): claim each row's key, update or
    create its entity/link, flushing `state.pending_links` every `batch_size` rows and once more
    after the loop (`_flush_pending`'s docstring has the ordering reason)."""
    state = _UpsertState()

    with session.no_autoflush:
        for i, row in enumerate(records):
            record_id = str(_row_get(row, "record_id"))

            source_record_id, existing_link, fields = _claim_source_record_key(
                ctx, state, row, record_id, dup_naturals
            )

            if existing_link is not None:
                _update_existing_entity(ctx, existing_link, row, fields)
            else:
                existing_link = _create_entity_and_link(session, ctx, state, row, source_record_id, fields)

            ctx.record_id_to_internal[record_id] = getattr(existing_link, ctx.fk_name)

            if (i + 1) % batch_size == 0:
                _flush_pending(session, state.pending_links)

    _flush_pending(session, state.pending_links)


def _load_one_event(
    session: Session,
    ctx: _LoadContext,
    ev: Mapping[str, Any],
    existing_event_keys: set[str],
) -> None:
    """One row of the events step: resolve the event's subject (via `ctx.record_id_to_internal`,
    or the stored link for a `removed` record), then write it unless already recorded (module
    docstring, "Change-event identity").

    A `removed` row (2026-10-10, docs/51 §2.7 item 1) is a public `withdrawn` event only when the
    source declares that meaning (`ctx.removal_meaning`). Where the connector announces removals
    (`ctx.announce_removals`) and the meaning is `unknown`, it is the public `delisted` event:
    published like any change event, `after = {"source_id", "register_name", "reason": "not
    stated"}`, `reason` the sentence every surface prints, no `before` and no `changed_keys`
    (nothing about the record changed but its presence in the file). Not when the same project is
    still in this frame under another key (`ctx.project_root`): that re-key is a
    `removed_from_source` with `after.project_root` and a reason saying so. Otherwise it is a
    `removed_from_source` event with `published_at`/`public_at` NULL and the declared meaning in
    `after`, which no public or paid surface serves (`services.db.models.NON_PUBLIC_EVENT_TYPES`).
    In every case the link's `gone_at` is set and the record's lifecycle state is left as the source
    last stated it."""
    source, kind, run = ctx.source, ctx.kind, ctx.run
    diff_type = str(ev["event_type"])
    record_id = str(ev["record_id"])
    subject_id = ctx.record_id_to_internal.get(record_id)
    if subject_id is None:
        # A `removed` record is, by definition, not in this frame: find it by its link.
        link = _link_for_event_record_id(ctx.cache, source, record_id)
        subject_id = getattr(link, ctx.fk_name) if link is not None else None
    if subject_id is None:
        ctx.result.warnings.append(f"event for unknown record_id {record_id!r} skipped")
        return
    event_type = (
        removal_event_type(ctx.removal_meaning, ctx.announce_removals)
        if diff_type == "removed"
        else DIFF_EVENT_TYPE_MAP.get(diff_type, "field_changed")
    )
    rekeyed_root: str | None = None
    if event_type == DELISTED_EVENT_TYPE:
        root = ctx.project_root(_record_key(source, record_id))
        if root in ctx.current_project_roots:
            # The same project is still listed under another key: a re-key, not a departure.
            event_type, rekeyed_root = REMOVED_FROM_SOURCE_EVENT_TYPE, root
    unpublished_removal = event_type == REMOVED_FROM_SOURCE_EVENT_TYPE
    announced_removal = event_type == DELISTED_EVENT_TYPE
    observed_at = _to_datetime(ev.get("observed_at")) or ctx.now
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
        ctx.result.events_skipped_idempotent += 1
        return

    # Provenance quartet (audit 2026-09-30 F8): the event names the record's own page, as its link
    # row does, not the manifest's landing URL; `retrieved_at` is the fetch the change was seen in.
    link = ctx.cache.links_by_entity.get(subject_id)
    if link is None:
        link = _link_for_event_record_id(ctx.cache, source, record_id)
    event_source_url = str(getattr(link, "source_url", None) or source.url)

    published_at: dt.datetime | None = ctx.now
    # A change event is public the moment it is published, like the record it belongs to
    # (owner, 2026-09-21: the ISO change-event delay is dropped, and its per-source knob
    # with it -- `services/ingest/lag.py` argues why the knob went too). `public_at` stays
    # a stored column because `services/api/visibility.py` reads it; it is now always
    # `published_at`. A removal is published only as a declared `withdrawn` or an announced
    # `delisted`; every other removal is stored unpublished below.
    public_at: dt.datetime | None = record_public_at(ctx.now)
    before_payload = {field_name: before_val} if before_val is not None else None
    after_payload = {field_name: after_val} if after_val is not None else None
    changed_keys = [str(field_name)]
    reason: str | None = None
    if announced_removal:
        # Public like any change event (timestamps above); the payload names the register and says
        # the register gives no reason. The status the source last stated stays on the record.
        before_payload = None
        after_payload = {
            "source_id": source.id,
            "register_name": ctx.register_name,
            "reason": DELISTED_REASON,
        }
        changed_keys = []
        reason = delisted_sentence(ctx.register_name)
    elif unpublished_removal:
        published_at = public_at = None
        after_payload = {"removal_meaning": ctx.removal_meaning}
        reason = (
            "no longer in the source's file; the source does not say this means withdrawal "
            f"(declared meaning: {ctx.removal_meaning})"
        )
        if rekeyed_root is not None:
            after_payload["project_root"] = rekeyed_root
            reason = (
                "re-keyed within the register: no longer in the source's file under this key, but "
                f"the same project ({rekeyed_root}) is still listed under another key"
            )
    event = Event(
        subject_type=kind,
        subject_id=subject_id,
        event_type=event_type,
        observed_at=observed_at,
        published_at=published_at,
        public_at=public_at,
        source_id=source.id,
        source_url=event_source_url,
        retrieved_at=observed_at,
        licence_id=source.licence_id,
        before=before_payload,
        after=after_payload,
        changed_keys=changed_keys,
        actor_type="pipeline",
        reason=reason,
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
    ctx.result.events_created += 1
    if unpublished_removal:
        ctx.result.removals_unpublished += 1
    if rekeyed_root is not None:
        ctx.result.removals_rekeyed += 1
    if announced_removal:
        ctx.result.removals_announced += 1

    if diff_type == "removed":
        link = ctx.cache.links_by_entity.get(subject_id)
        if link is not None:
            link.gone_at = observed_at


def _load_events(
    session: Session,
    ctx: _LoadContext,
    events_df: pd.DataFrame | None,
) -> None:
    """`load_dataframe`'s events step (module phase map block 19): preload existing idempotency
    keys once, then write each event that isn't already recorded (`_load_one_event`)."""
    if events_df is None or not len(events_df):
        return
    #: Preloaded once (Sprint 3 bulk-insert pass) instead of one `SELECT` per event; a key
    #: added below as each event is created keeps a later duplicate in the same call correctly
    #: idempotent without a re-query. `Event.source_id` is always this call's `source.id` for
    #: every key this loader writes (set explicitly below), so filtering on it is exact, not
    #: an approximation.
    existing_event_keys: set[str] = set(
        session.scalars(select(Event.idempotency_key).where(Event.source_id == ctx.source.id))
    )
    for ev in events_df.to_dict("records"):
        _load_one_event(session, ctx, ev, existing_event_keys)


def load_dataframe(
    session: Session,
    source: Source,
    kind: Kind,
    records_df: pd.DataFrame,
    events_df: pd.DataFrame | None,
    *,
    run: SourceRun | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    removal_meaning: RemovalMeaning = "unknown",
    announce_removals: bool = False,
    register_name: str | None = None,
    project_root: Callable[[str], str] | None = None,
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

    `removal_meaning` is what a `removed` event means at this source (`Connector.removal_meaning`,
    which `load_from_files` reads through `connector_removal_meaning`). The default, `unknown`,
    stores every removal as a non-public `removed_from_source` event: a direct caller that does not
    say otherwise never publishes a disappearance as a withdrawal.

    `announce_removals` (`Connector.announce_removals`, read by `load_from_files` through
    `connector_announces_removals`) publishes a removal of `unknown` meaning as `delisted`, worded
    with `register_name` (`Connector.register_name`; the source's operator, else its name, when
    none is given). Default False: a direct caller never announces a removal. A removal whose
    project (`project_root`, `Connector.project_root`; default each key its own project) is still
    in `records_df` under another key is a re-key: stored as `removed_from_source` with
    `after.project_root`, never announced.

    An orchestrator over four steps (module phase map, docs/42 §5): `_prepare_load_context`,
    `_index_records`, `_upsert_records`, `_load_events` -- the last two sharing `_LoadContext`.
    """
    if kind not in GENERIC_LOAD_KINDS:
        raise KindRefused(f"{source.id}: load_dataframe writes only {GENERIC_LOAD_KINDS}, not {kind!r}")
    if removal_meaning not in REMOVAL_MEANINGS:
        raise ValueError(f"{source.id}: removal_meaning={removal_meaning!r} not in {REMOVAL_MEANINGS}")
    ctx = _prepare_load_context(
        session,
        source,
        kind,
        records_df,
        run,
        removal_meaning,
        announce_removals,
        register_name,
        project_root,
    )
    ctx.result.kind = kind
    records, dup_naturals = _index_records(records_df)
    _upsert_records(session, ctx, records, dup_naturals, batch_size)
    if kind == "proposal":
        # A record several sources feed takes each field by the survivorship rule, not from
        # whichever source loaded last (docs/22 §23): the update above wrote this source's row.
        ctx.result.survivorship = restate_survivorship(
            session, set(ctx.record_id_to_internal.values()), now=ctx.now
        )
        # Grid interconnection points (owner 2026-09-28; docs/21 §3.24): parsed from each active
        # link's own raw POI text, after the links are flushed and before events, so a re-load
        # moves a proposal whose register revised its POI. A source whose rows carry no POI field
        # (EIA-860M) links nothing.
        ctx.result.interconnection = link_source_points(session, source, ctx.cache.links.values())
    else:
        # A notice past its deadline is served `closed` whatever its frame says (docs/21 §7.2;
        # `services/ingest/opportunity_status.py`): the frame's status is as of its fetch.
        ctx.result.deadline_closed = close_past_deadline(
            session, ctx.now, ids=ctx.record_id_to_internal.values()
        ).closed
    _stamp_last_changed(ctx)
    _load_events(session, ctx, events_df)
    return ctx.result


def load_from_files(
    session: Session,
    source_id: str,
    ts: str,
    *,
    data_root: pathlib.Path = pathlib.Path("data"),
    registry: Registry | None = None,
    store: Store | None = None,
    kind: Kind | None = None,
) -> LoadResult | RetirementLoadResult:
    """Read `data/normalized/<source_id>/<ts>.parquet` (+ the matching `events/` file and, if
    present, the `runs/<source_id>/<ts>.json` record) and load them (docs/20 §3.2, §3.7).

    `store` is where those objects live — pass `pipeline.connectors.store.open_store()` to read
    the bucket a fetch on another host wrote to (`SNAPSHOT_STORE=s3`); without one, local files
    under `data_root` are read, as before.

    Raises `GateRefused` before touching any file if the source's registry entry is gated —
    independent of whatever the connector run already did (module docstring).

    The kind is the connector class's own (`generic_load_kind`); `kind` may name it only for a
    source with no connector class. A `document` source, or any kind but proposal/opportunity,
    raises `KindRefused` before any file is read (module docstring, "Kind") -- unless
    `SPECIALISED_LOADERS` names a loader of its own for it, which then loads the run instead.

    One `source_run` row per run (2026-09-27, `_run_for_load`): the load attaches to the row the
    scheduler recorded for the fetch, and writes one itself only when loaded standalone.

    The load records `ts` as `source.last_loaded_ts` in its own transaction (architect audit A6),
    which `runs_to_load` reads to replay runs whose load never committed.
    """
    registry = registry or Registry()
    entry = registry.get(source_id)
    _assert_not_gated(entry)
    special = specialised_loader(source_id)
    if special is not None:
        special_result: LoadResult | RetirementLoadResult = special(
            session, source_id, ts, data_root=data_root, registry=registry, store=store
        )
        _mark_loaded(session, source_id, ts)
        return special_result
    load_kind = generic_load_kind(registry, source_id, kind)

    store = store if store is not None else Store(data_root)
    normalized_path = store.normalized_path(source_id, ts)
    events_path = store.events_path(source_id, ts)
    run_path = store.run_path(source_id, ts)
    if not store.exists(normalized_path):
        raise FileNotFoundError(store.locate(normalized_path))

    records_df = store.read_parquet(normalized_path)
    events_df = store.read_parquet(events_path) if store.exists(events_path) else None
    run_record = store.read_json(run_path) if store.exists(run_path) else None

    source = upsert_licence_and_source(session, entry, registry.version)
    run = _run_for_load(session, source, run_record, records_df, events_df, entry.egress)

    result = load_dataframe(
        session,
        source,
        load_kind,
        records_df,
        events_df,
        run=run,
        removal_meaning=connector_removal_meaning(registry, source_id),
        announce_removals=connector_announces_removals(registry, source_id),
        register_name=connector_register_name(registry, source_id),
        project_root=connector_project_root(registry, source_id),
    )
    result.source_run_id = run.id
    _mark_loaded(session, source_id, ts)
    return result


def _mark_loaded(session: Session, source_id: str, ts: str) -> None:
    """Records `ts` as the source's last loaded run, in the load's own transaction, so it is set
    exactly when the load commits. Never moves backwards (`Source.last_loaded_ts`)."""
    source = session.get(Source, source_id)
    if source is not None and (source.last_loaded_ts is None or ts > source.last_loaded_ts):
        source.last_loaded_ts = ts
        session.flush()


def _run_ts(record: Mapping[str, Any]) -> str | None:
    """The snapshot token a run record's normalised output was written under: the basename of
    `outputs.normalized` (a local path or an `s3://` URI, `Store.locate`)."""
    location = (record.get("outputs") or {}).get("normalized")
    if not location:
        return None
    name = pathlib.PurePosixPath(str(location)).name
    return name[: -len(".parquet")] if name.endswith(".parquet") else None


def runs_to_load(session: Session, store: Store, source_id: str, ts: str) -> list[str]:
    """The runs a load of `ts` must load, oldest first (architect audit 2026-09-30 A6).

    The runner diffs each run against the previous promoted snapshot, whether or not that snapshot
    was ever loaded. A failed load therefore lost its run's change events for good: the next run's
    events start from the failed run's state. So a load of `ts` first replays every promoted run
    (`status = ok` with a normalised output) after the source's `last_loaded_ts`, in order, then
    `ts` itself. Each loads exactly the events its run's diff produced, and event idempotency keys
    make a repeat a no-op.

    - No `last_loaded_ts` yet (a source first loaded, or loaded before migration 0032): `[ts]`.
    - `ts` older than `last_loaded_ts`: `[]`. A later run is loaded and `ts` was replayed before it;
      loading it again would move records back to an older state.
    - `ts` equal to `last_loaded_ts`: `[ts]`, an idempotent reload.
    - A promoted run whose normalised file is gone is skipped with a warning rather than blocking
      every later load."""
    source = session.get(Source, source_id)
    last = source.last_loaded_ts if source is not None else None
    if last is None or ts == last:
        return [ts]
    if ts < last:
        return []
    pending: list[str] = []
    for record in store.runs(source_id):
        run_ts = _run_ts(record)
        if run_ts is None or record.get("status") != "ok" or not (last < run_ts < ts):
            continue
        if not store.exists(store.normalized_path(source_id, run_ts)):
            log.warning(
                "unloaded run has no normalised file; its events cannot be replayed",
                extra={"source_id": source_id, "ts": run_ts},
            )
            continue
        pending.append(run_ts)
    return [*sorted(set(pending)), ts]


def _run_for_load(
    session: Session,
    source: Source,
    run_record: dict[str, Any] | None,
    records_df: pd.DataFrame,
    events_df: pd.DataFrame | None,
    egress: str,
) -> SourceRun:
    """The `source_run` row this load's events and data-quality warnings attach to: one row per
    run, keyed by the run record's id (2026-09-27).

    Canonical row: the one the scheduler writes for the fetch (`infra/scheduler/jobs.py`
    `record_source_run`, under the run record's id). It carries what the admin runs screen, the
    source's health and `GET /admin/v1/costs` read — the caller's trigger, the attempt, timings,
    the DQ verdict, the diff counts — and a DQ-hold release marks it by that id. When that row
    exists it is reused as it is; the loader adds only its own DQ warnings to it. Until this
    change the loader also inserted a second row, with a fresh id, for every loaded run, so each
    appeared twice on the runs screen and its `rows_changed` was counted twice in the costs.

    Loaded standalone (the CLI/dev path, `web/data_loading.py`; no scheduler, so no fetch row) the
    row is written here from the run record, under the record's id, so loading the same run again
    reuses it and a later `record_source_run` for it finds it already recorded. With no run record
    at all a row with a fresh id is written, as before."""
    from services.db.models import SOURCE_RUN_STATUSES, SOURCE_RUN_TRIGGERS

    record = run_record or {}
    run_id: _uuid.UUID | None
    try:
        run_id = _uuid.UUID(str(record["id"])) if record.get("id") else None
    except ValueError:
        run_id = None
    if run_id is not None:
        existing = session.get(SourceRun, run_id)
        if existing is not None:
            return existing
    # `scheduled` is what the scheduler wrote before migration 0025 fixed the vocabulary.
    trigger = str(record.get("trigger") or "manual")
    trigger = "schedule" if trigger == "scheduled" else trigger
    status = str(record.get("status") or "ok")
    run = SourceRun(
        source_id=source.id,
        trigger=trigger if trigger in SOURCE_RUN_TRIGGERS else "manual",
        started_at=_to_datetime(record.get("started_at")) or utcnow(),
        finished_at=_to_datetime(record.get("finished_at")),
        status=status if status in SOURCE_RUN_STATUSES else "ok",
        http_status=record.get("http_status"),
        bytes=record.get("bytes"),
        egress_class=record.get("egress_class", egress),
        rows_seen=record.get("rows_seen", len(records_df)),
        rows_new=record.get("rows_new", 0),
        rows_changed=record.get("rows_changed", 0),
        rows_gone=record.get("rows_gone", 0),
        events_emitted=record.get("events_emitted", len(events_df) if events_df is not None else 0),
        worker_seconds=record.get("worker_seconds", 0),
        dq_status=record.get("dq_status"),
        dq=record.get("dq"),
        attempt=int(record.get("attempt") or 1),
    )
    if run_id is not None:
        run.id = run_id
    session.add(run)
    session.flush()
    return run


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI over a tested function
    """`python -m services.ingest.loader --refresh-credits`: `refresh_all_licence_credits` against
    `DATABASE_URL`, committed. The only command this module offers; loads run through the scheduler."""
    import argparse
    import sys

    from services.db.session import get_engine, get_sessionmaker, session_scope

    parser = argparse.ArgumentParser(description="Infraque loader maintenance")
    parser.add_argument(
        "--refresh-credits", action="store_true", help="refresh licence credits from the manifest"
    )
    args = parser.parse_args(argv)
    if not args.refresh_credits:
        parser.print_help()
        return 2
    with session_scope(get_sessionmaker(get_engine())) as session:
        changed = refresh_all_licence_credits(session)
    sys.stdout.write(f"licence credits refreshed: {len(changed)} ({', '.join(changed) or 'none'})\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
