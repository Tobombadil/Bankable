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
unplaced, never dropped (docs/04 D-8). Never `exact`: that precision tier needs real coordinates
from the source itself (e.g. EIA-860M's raw `Latitude`/`Longitude`), which this module does not
promote — a documented follow-up, not silently done here.

Fixed since `services/resolve/README.md` first observed them (both without changing the public
functions below):
  - **Intra-run id reuse** (docs/22 §5/§7.1: the ISO-NE/NYISO signature). A source record whose
    natural key (`source_id`, `source_record_id`) already exists with different content from an
    *earlier* run is an update, handled exactly as before through the diff/event path — and a
    dataframe that repeats the *same* connector `record_id` for that key more than once in this
    call (e.g. several historical snapshots of one record bundled into one call) is likewise an
    ordinary sequential update, not reuse. Only a *different* `record_id` sharing that natural key
    inside the *same* call is true reuse, and those are never merged: the second (and any later)
    such occurrence has its `source_record_id` suffixed deterministically (`#2`, `#3`, ... — the
    same convention `pipeline.connectors.base.Connector.finalize` already applies to its own
    `record_id` for `dedupe_strategy = "suffix"` sources), keeping both rows, and a data-quality
    warning is recorded on `source_run.dq` and `LoadResult.warnings`.
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
import pathlib
import re
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

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
)
from services.ids import public_id, slugify
from services.ingest.geocode import CountyGazetteer, default_gazetteer, geocode
from services.ingest.lag import compute_public_at

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


def _is_derived_only_override(entry: SourceEntry) -> bool:
    if entry.reuse != "attribution":
        return False
    return bool(_DERIVED_ONLY_RE.search(entry.notes or "") or _DERIVED_ONLY_RE.search(entry.license or ""))

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


def _row_get(row: pd.Series, key: str) -> Any:
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
    return ts.to_pydatetime()


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


def _add_organization_alias_if_new(
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
    not duplicate the alias)."""
    alias_normalised = alias.lower()
    existing = session.scalar(
        select(OrganizationAlias).where(
            OrganizationAlias.organization_id == org.id,
            OrganizationAlias.alias_normalised == alias_normalised,
        )
    )
    if existing is not None:
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
    session.flush()


def _get_or_create_organization(
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
    """
    if not name or not str(name).strip():
        return None, False
    raw_name = str(name).strip()
    exact_key = raw_name.lower()

    org = session.scalar(select(Organization).where(Organization.name_normalised == exact_key))
    if org is not None:
        return org, False

    punct_key = _org_punct_key(raw_name)
    for candidate in session.scalars(select(Organization).where(Organization.merged_into_id.is_(None))):
        if _org_punct_key(candidate.name_canonical) == punct_key:
            _add_organization_alias_if_new(
                session,
                candidate,
                alias=raw_name,
                source=source,
                source_url=source_url,
                retrieved_at=retrieved_at,
            )
            return candidate, False

    org = Organization(
        public_id="",  # set below once we have the id
        slug="",
        name_canonical=raw_name,
        name_normalised=exact_key,
        type="other",
        country="US",
    )
    session.add(org)
    session.flush()
    org.public_id = public_id("org", org.id)
    org.slug = slugify(raw_name)
    if session.scalar(select(Organization).where(Organization.slug == org.slug, Organization.id != org.id)):
        # Defence in depth: two letter-distinct names should never coincidentally collide once
        # `_org_punct_key` above has already ruled out a punctuation-only match, but a lowest-cost
        # deterministic suffix here means a bug in that reasoning fails safe (no row, no crash)
        # rather than raising `UNIQUE constraint failed: organization.slug` at ingestion.
        org.slug = f"{org.slug}-{org.public_id[-6:].lower()}"
    session.flush()
    _add_organization_alias_if_new(
        session, org, alias=raw_name, source=source, source_url=source_url, retrieved_at=retrieved_at
    )
    return org, True


def _jurisdiction(state: str | None, source_jurisdiction: str) -> str:
    if state and len(state) == 2 and state.isalpha():
        country = source_jurisdiction.split("-")[0] if "-" in source_jurisdiction else "US"
        return f"{country}-{state.upper()}"
    return source_jurisdiction or "US"


def _get_or_create_location(
    session: Session,
    *,
    state: str | None,
    county: str | None,
    source: Source,
    retrieved_at: dt.datetime,
    derived_only: bool,
    gaz: CountyGazetteer | None = None,
) -> Location | None:
    """Geocode a connector-parsed state/county pair (docs/21 §3.7; docs/04 D-8): a resolvable
    county centroid, else a resolvable state centroid, else unplaced -- `services/ingest/geocode.py`
    does the lookup against the vendored public-domain county-centroid table so this no longer
    depends on `web/`'s copy of the same geocoder (services/README.md open decision #2).

    `derived_only` (the source's licence withholds raw/exact geo, docs/21 §8's "attribution, raw
    withheld" row, `_is_derived_only_override` above): stamps `precision_reason = "licence"`
    (docs/04 D-9) so the API can render the restricted-precision note. This never *upgrades* a
    location -- this module's geocoder only ever produces `county_centroid`/`state_centroid`/
    `unknown`, never `exact`, so there is nothing to downgrade from yet; the reason is recorded
    now so a future `exact`-capable geocoder (e.g. EIA-860M's raw `Latitude`/`Longitude`) does not
    silently start leaking an exact point for these sources without this check already in place.
    """
    if not state and not county:
        return None
    kind = "county" if county else "state"
    point, precision = geocode(state, county, gaz=gaz)
    loc = Location(
        kind=kind,
        geom=point,
        precision=precision,
        precision_reason="licence" if derived_only else None,
        county_name=county or None,
        state_code=(f"US-{state.upper()}" if state else None),
        country="US",
        source_id=source.id,
        source_url=source.url,
        retrieved_at=retrieved_at,
        licence_id=source.licence_id,
    )
    session.add(loc)
    session.flush()
    return loc


def _proposal_fields_from_row(row: pd.Series, source: Source) -> dict[str, Any]:
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


def _opportunity_fields_from_row(row: pd.Series) -> dict[str, Any]:
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


def load_dataframe(
    session: Session,
    source: Source,
    kind: Kind,
    records_df: pd.DataFrame,
    events_df: pd.DataFrame | None,
    *,
    run: SourceRun | None = None,
) -> LoadResult:
    """Upsert one connector run's normalised records and diff events (idempotent).

    `source` must already be loaded via `upsert_licence_and_source` (its gate re-checked there).
    Safe to call twice with the same `records_df`/`events_df`: `proposal_source`/
    `opportunity_source` rows are keyed on `(source_id, source_record_id)` and `event` rows on
    `idempotency_key`, so a re-run changes nothing (docs/20 §3, docs/21 §6.1).
    """
    now = utcnow()
    result = LoadResult(source_run_id=run.id if run else _uuid.UUID(int=0))
    record_id_to_internal: dict[str, _uuid.UUID] = {}
    #: raw source_record_id -> {connector record_id -> the (possibly suffixed) key stored for it}.
    #: A dataframe covering more than one historical pull for the same source (as
    #: `services/resolve/report.py`'s evaluation snapshot does) legitimately repeats the *same*
    #: `record_id` many times for one natural key -- each repeat is an ordinary sequential update
    #: and must keep using the same stored key. Only a *different* `record_id` sharing that same
    #: natural key is true intra-run id reuse (docs/22 §5/§7.1): the connector's own
    #: `dedupe_strategy = "suffix"` already gives such rows distinct `record_id`s (e.g.
    #: `nyiso:0031` vs `nyiso:0031#2`) precisely because they were simultaneous, distinct records
    #: at parse time, which is the signal this loader keys off rather than raw repetition count.
    variant_keys: dict[str, dict[str, str]] = {}

    entity_cls = Proposal if kind == "proposal" else Opportunity
    link_cls = ProposalSource if kind == "proposal" else OpportunitySource
    fk_name = "proposal_id" if kind == "proposal" else "opportunity_id"

    for _, row in records_df.iterrows():
        raw_source_record_id = str(_row_get(row, "source_record_id"))
        record_id = str(_row_get(row, "record_id"))
        variants = variant_keys.setdefault(raw_source_record_id, {})
        if record_id in variants:
            source_record_id = variants[record_id]
        else:
            occurrence = len(variants) + 1
            if occurrence == 1:
                source_record_id = raw_source_record_id
            else:
                # A different connector `record_id` reusing this natural key inside the same
                # run/dataframe -- never merge it with the earlier variant(s). Suffix the *stored*
                # key deterministically, the same convention
                # `pipeline.connectors.base.Connector.finalize` already applies to its own
                # `record_id` for `dedupe_strategy = "suffix"` sources, so the loader is
                # consistent with the connector layer rather than depending on it having done so.
                source_record_id = f"{raw_source_record_id}#{occurrence}"
                warning = (
                    f"{source.id}: source_record_id {raw_source_record_id!r} reused by a distinct "
                    f"record ({record_id!r}) within this run; stored as {source_record_id!r} "
                    "rather than merged into the earlier occurrence (docs/22 §5, §7.1)"
                )
                result.warnings.append(warning)
                _record_dq_warning(
                    run,
                    check="duplicate_source_record_id_same_run",
                    detail=warning,
                    data={
                        "source_record_id": raw_source_record_id,
                        "record_id": record_id,
                        "occurrence": occurrence,
                        "stored_as": source_record_id,
                    },
                )
            variants[record_id] = source_record_id
        retrieved_at = _to_datetime(_row_get(row, "retrieved_at")) or now

        existing_link = session.scalar(
            select(link_cls).where(
                link_cls.source_id == source.id,
                link_cls.source_record_id == source_record_id,
                link_cls.active.is_(True),
            )
        )
        raw_payload = _parse_raw(_row_get(row, "raw"))

        if kind == "proposal":
            fields = _proposal_fields_from_row(row, source)
        else:
            fields = _opportunity_fields_from_row(row)

        if existing_link is not None:
            entity = session.get(entity_cls, getattr(existing_link, fk_name))
            if entity is None:
                # `entity_cls`/`link_cls` are `type[Proposal] | type[Opportunity]` /
                # `type[ProposalSource] | type[OpportunitySource]`: mypy resolves `session.get`
                # and `select()` against the shared declarative `Base` for a union-of-classes
                # reference it cannot specialise, so every attribute below needs a targeted
                # ignore even though both concrete siblings share the exact same column. A
                # generic rewrite (one TypeVar-bound helper per kind) would remove these but
                # duplicates the whole function body; not worth it for two extra type params.
                raise RuntimeError(
                    f"proposal_source/opportunity_source row {existing_link.id} points at a "  # type: ignore[attr-defined]
                    "missing entity — this is a store consistency bug, not a data error"
                )
            for k, v in fields.items():
                setattr(entity, k, v)
            entity.last_changed = now  # type: ignore[attr-defined]
            existing_link.raw = raw_payload  # type: ignore[attr-defined]
            existing_link.normalised = _jsonable(  # type: ignore[attr-defined]
                {k: v for k, v in fields.items() if not isinstance(v, dict)}
            )
            existing_link.status_raw = fields.get("status_raw")  # type: ignore[attr-defined]
            existing_link.last_seen = retrieved_at  # type: ignore[attr-defined]
            existing_link.retrieved_at = retrieved_at  # type: ignore[attr-defined]
            if kind == "proposal":
                result.proposals_updated += 1
            else:
                result.opportunities_updated += 1
        else:
            published_at = now
            public_at = compute_public_at(
                published_at, kind, source_lag_days=source.lag_days, lag_overrides=source.lag_overrides
            )
            entity = entity_cls(
                public_id=public_id("prop" if kind == "proposal" else "opp", _uuid.uuid4()),
                slug="",
                publish_state="public",
                published_at=published_at,
                public_at=public_at,
                min_reuse_class=source.licence.reuse_class,
                source_count=1,
                **fields,
            )
            if isinstance(entity, Proposal):
                sponsor, sponsor_created = _get_or_create_organization(
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
                )
                if loc is not None:
                    entity.location_id = loc.id
                    result.locations_created += 1
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
            session.flush()
            entity.public_id = public_id("prop" if kind == "proposal" else "opp", entity.id)
            title = fields.get("name_canonical") or fields.get("title") or "record"
            entity.slug = f"{slugify(title)}-{entity.public_id[-6:].lower()}"
            session.flush()
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
            session.add(existing_link)
            session.flush()
            if kind == "proposal":
                result.proposals_created += 1
            else:
                result.opportunities_created += 1

        record_id_to_internal[record_id] = getattr(existing_link, fk_name)

    if events_df is not None and len(events_df):
        for _, ev in events_df.iterrows():
            diff_type = str(ev["event_type"])
            record_id = str(ev["record_id"])
            subject_id = record_id_to_internal.get(record_id)
            if subject_id is None:
                result.warnings.append(f"event for unknown record_id {record_id!r} skipped")
                continue
            event_type = DIFF_EVENT_TYPE_MAP.get(diff_type, "field_changed")
            observed_at = _to_datetime(ev.get("observed_at")) or now
            field_name = ev.get("field") or "lifecycle_state"
            before_val = ev.get("before")
            after_val = ev.get("after")
            after_hash = hashlib.sha1(  # noqa: S324 — identity key, not security
                json.dumps(after_val, default=str, sort_keys=True).encode()
            ).hexdigest()[:12]
            idempotency_key = f"{source.id}:{record_id}:{event_type}:{after_hash}"

            already = session.scalar(select(Event).where(Event.idempotency_key == idempotency_key))
            if already is not None:
                result.events_skipped_idempotent += 1
                continue

            published_at = now
            public_at = compute_public_at(
                published_at,
                kind,
                source_lag_days=source.lag_days,
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
            session.flush()
            result.events_created += 1

            if diff_type == "removed":
                link = session.scalar(
                    select(link_cls).where(
                        getattr(link_cls, fk_name) == subject_id, link_cls.active.is_(True)
                    )
                )
                if link is not None:
                    link.gone_at = observed_at  # type: ignore[attr-defined]  # see the note above

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
