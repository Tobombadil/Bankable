"""Organisation types from EIA-860 Schedule 1 "Utility Data" (docs/22 §27; docs/28 §8.2).

On 2026-10-23, 13,854 of the 13,856 public organisations were type `other` (docs/28 §3.3): every loader
creates an organisation as `other` and only the curated RFP issuers say otherwise, so the Companies page's
Type filter had nothing to filter. EIA states a type for every utility that files Form EIA-860: its annual
zip's `1___Utility_Y<year>.xlsx` gives each `Utility ID` an `Entity Type` code (C cooperative, I
investor-owned, Q independent power producer, M municipal, P political subdivision, S state, F federal, IND
industrial, COM commercial). The zip is already fetched and stored (`us.eia.860`, the Schedule 4 owner
connector); until this module nothing read Schedule 1 from it.

**The read.** `parse_utility_sheet` keeps three columns, `Utility ID`, `Utility Name` and `Entity Type`; the
street address, city, state, ZIP and relationship flags are dropped at parse and never stored. A row whose
name has a natural person's shape (`services/personal_names.py`; 15 of the 6,946 rows of 2025) is dropped
there too: its name is never written anywhere, and no organisation is typed from it.

**The join**, deterministic first (docs/22 §4):
1. *EIA utility id.* An organisation that sponsors an EIA-860M record holds that record's `Entity ID` on the
   link's raw row (the monthly and the annual forms share EIA's utility ids): the code is that utility's.
   The id counts only when the link's own `Entity Name` is the sponsor's, by `org_key` over its names and
   aliases: a merged record's sponsor can be another register's (an ERCOT interconnecting entity).
2. *Name.* Otherwise `pipeline.normalize.org_key` of `Utility Name` against every live organisation's name
   and aliases (`org_key_multimap`), the key every loader uses; EIA-860M and Schedule 4 write EIA's own
   spelling of the utility's name, so the key meets the row it came from.
An organisation whose matches carry codes that map to more than one type is left as it is and counted
(`conflicts`), and so is one matched by name alone whose every sponsored record and owned asset is outside
the US (`foreign_skipped`: EIA files US utilities only, and the key cannot tell a GB `X Limited` from a US
`X LLC`). The code-to-type table is `data/vendored/organizations/eia860_entity_types.yaml`.

**What it changes, and what it never touches.** Only an organisation whose type is `other`, or whose type
this module set and EIA now states differently: a curated issuer's type, an editor's and any other
loader's stand. A natural person's row (`personal_data`) is never typed. Each change is one
`type_classified` event on the organisation (`before.type`, `after.type`, the EIA code, the utility ids,
how it matched, the table's digest, the schedule year), with the snapshot's provenance quartet (`us.eia.860`,
its URL, `retrieved_at` and licence) and no public timestamp: not news. `revert(event_id)` restores
`before.type` and writes `type_reverted`; a manual revert is never re-applied.

**Where it runs.** `services/resolve/merge.py::resolve_organizations` calls `apply_from_store` after its
merges, so the scheduler's `resolve_tick` (after every load, and daily) types every stored organisation,
the 13,856 already held included, and the companies index refreshes in the same pass. Without a stored
`us.eia.860` snapshot it does nothing. `python -m services.resolve.org_types` runs it by hand (`--archive`
reads a downloaded zip instead of the store; `--dry-run` writes nothing).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import functools
import hashlib
import io
import json
import logging
import os
import pathlib
import re
import uuid as _uuid
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.base import ParseError
from pipeline.normalize import org_key
from services.db.models import Event, Licence, Organization, Proposal, ProposalSource, Source, utcnow
from services.ingest.loader import ORGANIZATION_TYPES
from services.ingest.org_redirects import OrgRedirects
from services.ingest.organizations import evidence_countries, org_key_multimap
from services.personal_names import classify
from services.resolve import provenance

log = logging.getLogger(__name__)

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAPPING_PATH = ROOT / "data" / "vendored" / "organizations" / "eia860_entity_types.yaml"

SOURCE_ID = "us.eia.860"
#: The EIA-860M source whose links carry the `Entity ID` of the organisation they name as sponsor.
MONTHLY_SOURCE_ID = "us.eia.860m"
ENTITY_ID_FIELD, ENTITY_NAME_FIELD = "Entity ID", "Entity Name"
UTILITY_MEMBER_RE = re.compile(r"^1_+Utility_Y(\d{4})\.xlsx$", re.I)
SHEET = "Utility"
#: Row 0 is the schedule's title ("2025 Form EIA-860 Data - Schedule 1, 'Utility Data'"), row 1 the header.
HEADER_ROW = 1
ID_COLUMN, NAME_COLUMN, CODE_COLUMN = "Utility ID", "Utility Name", "Entity Type"
#: The only columns read. Address, city, state, ZIP and the relationship flags are never kept.
KEEP_COLUMNS = (ID_COLUMN, NAME_COLUMN, CODE_COLUMN)
DEFAULT_TYPE = "other"

TYPE_EVENT = "type_classified"
REVERT_EVENT = "type_reverted"
EVENT_TYPES = (TYPE_EVENT, REVERT_EVENT)
#: Utility ids kept on one event (an organisation key can gather many municipal utilities' rows).
MAX_IDS_ON_EVENT = 20
CHUNK = 500


# ------------------------------------------------------------------------------------------- mapping
@dataclass(frozen=True)
class EntityTypeMapping:
    version: str
    types: Mapping[str, str]
    meanings: Mapping[str, str]

    def type_for(self, code: str) -> str | None:
        return self.types.get(code.strip().upper())


def parse_mapping(doc: Mapping[str, Any], allowed: Iterable[str] | None = None) -> EntityTypeMapping:
    codes = doc.get("codes")
    if not isinstance(codes, Mapping) or not codes:
        raise ValueError("the entity-type table lists no codes")
    allowed_set = set(allowed) if allowed is not None else None
    types: dict[str, str] = {}
    meanings: dict[str, str] = {}
    for code, row in codes.items():
        if not isinstance(row, Mapping) or not row.get("type"):
            raise ValueError(f"entity type {code!r} has no target type")
        target = str(row["type"])
        if allowed_set is not None and target not in allowed_set:
            raise ValueError(f"entity type {code!r} maps to {target!r}, not an organisation type")
        types[str(code).strip().upper()] = target
        meanings[str(code).strip().upper()] = str(row.get("eia") or "")
    digest = hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return EntityTypeMapping(version=digest[:12], types=types, meanings=meanings)


@functools.lru_cache(maxsize=2)
def load_mapping(path: pathlib.Path = MAPPING_PATH) -> EntityTypeMapping:
    with path.open(encoding="utf-8") as fh:
        return parse_mapping(yaml.safe_load(fh) or {}, allowed=ORGANIZATION_TYPES)


# ------------------------------------------------------------------------------------------- the read
def find_utility_member(names: Iterable[str]) -> tuple[str, int] | None:
    """`(member, data year)` of the Schedule 1 workbook in an EIA-860 zip, the newest year if several."""
    found = [(n, int(m.group(1))) for n in names if (m := UTILITY_MEMBER_RE.match(pathlib.PurePath(n).name))]
    return max(found, key=lambda t: t[1]) if found else None


def _id_text(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        return text
    return str(int(number)) if number.is_integer() else text


def parse_utility_sheet(content: bytes) -> pd.DataFrame:
    """Schedule 1 as `utility_id`, `utility_name`, `entity_type` (module docstring): three columns only,
    natural persons' rows dropped. Raises `ParseError` when the sheet or a kept column is missing."""
    try:
        frame = pd.read_excel(
            io.BytesIO(content), sheet_name=SHEET, header=HEADER_ROW, dtype=str, usecols=list(KEEP_COLUMNS)
        )
    except ValueError as exc:  # a missing sheet or column
        raise ParseError(f"EIA-860 Schedule 1 layout changed: {exc}") from exc
    out = pd.DataFrame(
        {
            "utility_id": [_id_text(v) for v in frame[ID_COLUMN]],
            "utility_name": [str(v).strip() if isinstance(v, str) else "" for v in frame[NAME_COLUMN]],
            "entity_type": [str(v).strip().upper() if isinstance(v, str) else "" for v in frame[CODE_COLUMN]],
        }
    )
    out = out[(out["utility_id"] != "") & (out["utility_name"] != "")]
    personal = out["utility_name"].map(lambda n: classify(n).personal)
    out = out[~personal].reset_index(drop=True)
    out.attrs["personal_dropped"] = int(personal.sum())
    return out


def read_archive(zip_bytes: bytes) -> tuple[pd.DataFrame, int]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        member = find_utility_member(zf.namelist())
        if member is None:
            raise ParseError("no 1___Utility_Y<year>.xlsx in the EIA-860 zip")
        return parse_utility_sheet(zf.read(member[0])), member[1]


@dataclass(frozen=True)
class Schedule1:
    """The parsed sheet and the stored snapshot it came from (the provenance of every event)."""

    utilities: pd.DataFrame
    year: int
    source_url: str
    retrieved_at: dt.datetime
    licence_id: str | None
    sha256: str | None = None


def _instant(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


#: The parsed sheet per snapshot digest: the resolve pass runs after every load, and the 23 MB zip (an
#: object-store read in production) changes once a year, so it is read and parsed once per process.
_PARSED: dict[str, tuple[pd.DataFrame, int]] = {}


def latest_schedule1(store: Any = None) -> Schedule1 | None:
    """Schedule 1 from the latest stored `us.eia.860` snapshot (`pipeline.connectors.store`), or None."""
    from pipeline.connectors.store import open_store

    st = store if store is not None else open_store()
    previous = st.last_snapshot(SOURCE_ID)
    if previous is None:
        return None
    snap = previous.record.get("snapshot") or {}
    digest = previous.sha256
    parsed = _PARSED.get(digest) if digest else None
    if parsed is None:
        parsed = read_archive(previous.content())
        if digest:
            _PARSED.clear()
            _PARSED[digest] = parsed
    utilities, year = parsed
    return Schedule1(
        utilities=utilities,
        year=year,
        source_url=str(snap.get("fetched_url") or previous.record.get("url") or ""),
        retrieved_at=_instant(snap.get("retrieved_at") or previous.record.get("finished_at")),
        licence_id=snap.get("licence_id"),
        sha256=digest or None,
    )


# ------------------------------------------------------------------------------------------- the join
@dataclass
class OrgTypeReport:
    considered: int = 0
    typed: int = 0
    retyped: int = 0
    reverted_to_other: int = 0
    kept: int = 0
    set_elsewhere: int = 0
    cleared_elsewhere: int = 0
    suppressed: int = 0
    personal_skipped: int = 0
    conflicts: int = 0
    unmatched: int = 0
    stays_other: int = 0
    by_match: Counter[str] = field(default_factory=Counter)
    by_code: Counter[str] = field(default_factory=Counter)
    types_before: Counter[str] = field(default_factory=Counter)
    types_after: Counter[str] = field(default_factory=Counter)
    #: Organisations both joins reach: the name join's type equals the utility id's / differs from it.
    id_name_agree: int = 0
    id_name_disagree: int = 0
    #: Name matches refused because every record the organisation sponsors or owns is outside the US.
    foreign_skipped: int = 0
    personal_rows_dropped: int = 0
    schedule_year: int | None = None
    #: Why nothing was written, when nothing could be (no source row to cite).
    skipped: str | None = None
    events: list[Event] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in dataclasses.fields(self):
            if f.name == "events":
                continue
            value = getattr(self, f.name)
            out[f.name] = (
                dict(sorted(value.items(), key=lambda kv: (-kv[1], kv[0])))
                if isinstance(value, Counter)
                else value
            )
        return out


def _entity_ids_by_sponsor(
    session: Session, redirects: OrgRedirects, keys_of: Mapping[_uuid.UUID, set[str]]
) -> dict[_uuid.UUID, set[str]]:
    """The EIA utility ids each organisation sponsors an EIA-860M record under (join 1). A record's
    sponsor may have come from another register merged into it (an ERCOT interconnecting entity), so an
    id counts only when the link's own `Entity Name` is that organisation's, by the same key."""
    out: dict[_uuid.UUID, set[str]] = defaultdict(set)
    rows = session.execute(
        select(Proposal.sponsor_org_id, ProposalSource.raw)
        .join(ProposalSource, ProposalSource.proposal_id == Proposal.id)
        .where(
            ProposalSource.source_id == MONTHLY_SOURCE_ID,
            ProposalSource.active.is_(True),
            Proposal.sponsor_org_id.is_not(None),
        )
    )
    for sponsor_id, raw in rows:
        payload = raw or {}
        uid = _id_text(payload.get(ENTITY_ID_FIELD))
        name = payload.get(ENTITY_NAME_FIELD)
        sponsor = redirects.terminal_id(sponsor_id)
        if uid and isinstance(name, str) and org_key(name) in keys_of.get(sponsor, set()):
            out[sponsor].add(uid)
    return out


def _history(session: Session, ids: list[_uuid.UUID]) -> dict[_uuid.UUID, list[Event]]:
    out: dict[_uuid.UUID, list[Event]] = {}
    for i in range(0, len(ids), CHUNK):
        for event in session.scalars(
            select(Event)
            .where(
                Event.subject_type == "organization",
                Event.event_type.in_(EVENT_TYPES),
                Event.subject_id.in_(ids[i : i + CHUNK]),
            )
            .order_by(Event.seq)
        ):
            out.setdefault(event.subject_id, []).append(event)
    return out


def _quartet(session: Session, schedule: Schedule1) -> provenance.Quartet | None:
    """The snapshot's quartet: the licence it was stored under while that licence row exists, else the
    source's current one. None without a `us.eia.860` source row or a URL (nothing is then written)."""
    source = session.get(Source, SOURCE_ID)
    if source is None or not schedule.source_url:
        return None
    licence_id = schedule.licence_id
    if licence_id is None or session.get(Licence, licence_id) is None:
        licence_id = source.licence_id
    return provenance.Quartet(SOURCE_ID, schedule.source_url, schedule.retrieved_at, licence_id)


def _event(
    org: Organization,
    *,
    event_type: str,
    before: dict[str, Any],
    after: dict[str, Any],
    quartet: provenance.Quartet | None,
    reason: str,
    sequence: int,
    actor_type: str = "pipeline",
    reverses: Event | None = None,
) -> Event:
    return Event(
        subject_type="organization",
        subject_id=org.id,
        event_type=event_type,
        observed_at=utcnow(),
        **(quartet.columns() if quartet is not None else {}),
        before=before,
        after=after,
        changed_keys=["type"],
        actor_type=actor_type,
        confidence=1.0,
        reason=reason,
        reverses_event_id=reverses.id if reverses is not None else None,
        idempotency_key=f"org_type:{org.id}:{sequence}",
    )


def apply_entity_types(
    session: Session,
    schedule: Schedule1,
    *,
    mapping: EntityTypeMapping | None = None,
    dry_run: bool = False,
) -> OrgTypeReport:
    """Type every live organisation EIA Schedule 1 names (module docstring)."""
    table = mapping if mapping is not None else load_mapping()
    report = OrgTypeReport(schedule_year=schedule.year)
    report.personal_rows_dropped = int(schedule.utilities.attrs.get("personal_dropped", 0))
    code_by_id: dict[str, str] = {}
    codes_by_key: dict[str, set[str]] = defaultdict(set)
    ids_by_key: dict[str, set[str]] = defaultdict(set)
    for uid, name, code in schedule.utilities[["utility_id", "utility_name", "entity_type"]].itertuples(
        index=False
    ):
        if not code:
            continue
        code_by_id[uid] = code
        key = org_key(name)
        codes_by_key[key].add(code)
        ids_by_key[key].add(uid)

    redirects = OrgRedirects(session)
    index = org_key_multimap(session)
    every_key: dict[_uuid.UUID, set[str]] = defaultdict(set)
    keys_by_org: dict[_uuid.UUID, set[str]] = defaultdict(set)
    for key, members in index.items():
        for org in members:
            every_key[org.id].add(key)
            if key in codes_by_key:
                keys_by_org[org.id].add(key)
    by_sponsor = _entity_ids_by_sponsor(session, redirects, every_key)
    countries = evidence_countries(session)

    orgs = list(session.scalars(select(Organization).where(Organization.merged_into_id.is_(None))))
    history = _history(session, [o.id for o in orgs])
    quartet = _quartet(session, schedule)
    if quartet is None and not dry_run:
        report.skipped = f"no {SOURCE_ID} source row" if session.get(Source, SOURCE_ID) is None else "no URL"
        log.warning("org types: %s; nothing typed (every change must cite its source)", report.skipped)
        return report
    for org in orgs:
        report.considered += 1
        report.types_before[org.type] += 1
        past = history.get(org.id, [])
        last = past[-1] if past else None
        ours = (
            last is not None and last.event_type == TYPE_EVENT and (last.after or {}).get("type") == org.type
        )
        if org.type != DEFAULT_TYPE and not ours:
            report.set_elsewhere += 1
            report.types_after[org.type] += 1
            continue
        if last is not None and last.event_type == TYPE_EVENT and not ours:
            # This pass typed it and someone set it back to `other` without `revert`: theirs stands.
            report.cleared_elsewhere += 1
            report.types_after[org.type] += 1
            continue
        if last is not None and last.event_type == REVERT_EVENT and (last.after or {}).get("suppress"):
            report.suppressed += 1
            report.types_after[org.type] += 1
            continue
        if org.personal_data:
            report.personal_skipped += 1
            report.types_after[org.type] += 1
            continue
        id_codes = {code_by_id[u] for u in by_sponsor.get(org.id, ()) if u in code_by_id}
        name_codes: set[str] = set().union(*(codes_by_key[k] for k in keys_by_org.get(org.id, ())))
        abroad = bool(countries.get(org.id)) and "US" not in countries[org.id]
        if name_codes and abroad and not id_codes:
            # A name key cannot tell a GB `X Limited` from a US `X LLC`; EIA files US utilities only.
            report.foreign_skipped += 1
            report.types_after[org.type] += 1
            continue
        if id_codes:
            codes, how = id_codes, "entity_id"
            ids = sorted(u for u in by_sponsor.get(org.id, ()) if u in code_by_id)
            if name_codes:
                same = {table.type_for(c) for c in name_codes} == {table.type_for(c) for c in id_codes}
                report.id_name_agree += int(same)
                report.id_name_disagree += int(not same)
        elif name_codes:
            codes, how = name_codes, "name"
            ids = sorted(set().union(*(ids_by_key[k] for k in keys_by_org.get(org.id, ()))))
        else:
            report.unmatched += 1
            report.types_after[org.type] += 1
            continue
        targets = {table.type_for(c) for c in codes}
        if None in targets or len(targets) != 1:
            report.conflicts += 1
            report.types_after[org.type] += 1
            continue
        target = next(iter(targets))
        assert target is not None  # noqa: S101 -- None excluded above
        report.by_match[how] += 1
        for code in codes:
            report.by_code[code] += 1
        if target == org.type:
            report.kept += int(ours)
            report.stays_other += int(not ours)
            report.types_after[org.type] += 1
            continue
        before_type = org.type
        report.types_after[target] += 1
        if target == DEFAULT_TYPE:
            report.reverted_to_other += 1
        elif ours:
            report.retyped += 1
        else:
            report.typed += 1
        if dry_run:
            continue
        org.type = target
        org.last_changed = utcnow()
        event = _event(
            org,
            event_type=TYPE_EVENT,
            before={"type": before_type},
            after={
                "type": target,
                "eia_entity_type": sorted(codes),
                "eia_utility_ids": ids[:MAX_IDS_ON_EVENT],
                "eia_utility_id_count": len(ids),
                "match": how,
                "mapping_version": table.version,
                "schedule_year": schedule.year,
            },
            quartet=quartet,
            reason=f"EIA-860 Schedule 1 entity type {'/'.join(sorted(codes))} ({how})",
            sequence=len(past) + 1,
        )
        session.add(event)
        session.flush()
        report.events.append(event)
    return report


def apply_from_store(session: Session, *, store: Any = None) -> OrgTypeReport | None:
    """`apply_entity_types` over the latest stored Schedule 1; None (and nothing written) without one."""
    schedule = latest_schedule1(store)
    if schedule is None:
        log.info("org types: no stored %s snapshot; nothing typed", SOURCE_ID)
        return None
    return apply_entity_types(session, schedule)


def revert(session: Session, event_id: _uuid.UUID, *, reason: str = "revert") -> Event:
    """Undo one `type_classified` event: the organisation's type goes back to `before.type`, and the
    pass never types it again. Idempotent; refuses an event that is not the organisation's latest."""
    event = session.get(Event, event_id)
    if event is None or event.event_type != TYPE_EVENT:
        raise ValueError(f"{event_id} is not a {TYPE_EVENT} event")
    existing = session.scalar(select(Event).where(Event.reverses_event_id == event.id))
    if existing is not None:
        return existing
    org = session.get(Organization, event.subject_id)
    if org is None:
        raise ValueError(f"organisation {event.subject_id} is gone")
    past = _history(session, [org.id]).get(org.id, [])
    if not past or past[-1].id != event.id:
        raise ValueError(f"{event_id} is not the latest type event of organisation {org.id}")
    previous = str((event.before or {}).get("type") or DEFAULT_TYPE)
    current = org.type
    org.type = previous
    org.last_changed = utcnow()
    out = _event(
        org,
        event_type=REVERT_EVENT,
        before={"type": current},
        after={"type": previous, "suppress": True},
        quartet=provenance.of_event(event),
        reason=reason,
        sequence=len(past) + 1,
        actor_type="user",
        reverses=event,
    )
    session.add(out)
    session.flush()
    return out


# ------------------------------------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m services.resolve.org_types",
        description="Type organisations from EIA-860 Schedule 1 (docs/22 §27).",
    )
    parser.add_argument("--db", type=pathlib.Path, default=None, help="SQLite path; default DATABASE_URL")
    parser.add_argument("--archive", type=pathlib.Path, default=None, help="an eia860<year>.zip to read")
    parser.add_argument("--url", default="", help="with --archive: the URL the zip was downloaded from")
    parser.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    args = parser.parse_args(argv)
    from services.db.session import get_engine, get_sessionmaker

    url = f"sqlite+pysqlite:///{args.db}" if args.db else os.environ.get("DATABASE_URL")
    if not url:
        parser.error("set DATABASE_URL or pass --db")
    if args.archive is not None:
        utilities, year = read_archive(args.archive.read_bytes())
        modified = dt.datetime.fromtimestamp(args.archive.stat().st_mtime, dt.UTC)
        schedule: Schedule1 | None = Schedule1(utilities, year, args.url, modified, None)
    else:
        schedule = latest_schedule1()
    if schedule is None:
        parser.error(f"no stored {SOURCE_ID} snapshot; pass --archive")
    with get_sessionmaker(get_engine(url))() as session:
        report = apply_entity_types(session, schedule, dry_run=args.dry_run)
        if not args.dry_run:
            session.commit()
    print(json.dumps(report.as_dict(), default=str))  # noqa: T201 -- CLI summary line
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
