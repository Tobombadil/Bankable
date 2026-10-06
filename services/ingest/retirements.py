"""Load a `us.eia.860m.retirements` run onto the `power_plant` assets: their retirement state, and
an event for every real change (lane R1, owner request 2026-10-06; `docs/27` section R1).

Input: what `pipeline/connectors/runner.py` wrote for one run -- the normalised generator frame
(`pipeline/connectors/us_eia_860m_retirements/connector.py`) and, when the run had a previous
snapshot to diff against, its events frame (`pipeline/diff.py`).

Two steps, both idempotent:

1. **Assets.** The generators are summarised per EIA plant id by
   `pipeline/context/retirements.py::summarise_plants`, the same function
   `pipeline/context/eia_plants.py` uses to build the asset frame, and each existing `power_plant`
   asset keyed on that plant id (source `us.eia.860m`) gets the result: `status`, `retirement_year`
   and `attributes["retirement"]`. A row is touched only when one of the three differs, so a
   re-load of the same run changes nothing and `last_changed` moves only on real change. A plant
   with no asset yet is counted, not created: creating assets (with coordinates, operator and the
   rest) is the context loader's job, and a retired plant gets its asset there.

2. **Events.** One `event` row (`subject_type = "asset"`) per plant and change type in the run,
   from the generator-level diff:

   | diff on a generator                                   | asset event               |
   |-------------------------------------------------------|---------------------------|
   | state -> `retiring` (from operating or standby)       | `retirement_planned`      |
   | `retiring` -> operating or standby                    | `retirement_cancelled`    |
   | any in-service state -> `retired`                     | `retired`                 |
   | `retired` -> any in-service state                     | `returned_to_service`     |
   | `cod_change` on a `retiring` generator, both dates set| `retirement_date_changed` |
   | new generator already `retiring`                      | `retirement_planned`      |
   | new generator `retired` in the observed or prior year | `retired`                 |

   Everything else is not retirement news and writes nothing: operating <-> standby flips,
   capacity re-ratings, generators appearing in service or disappearing. A first run has no events
   file at all (the runner diffs only against a previous snapshot), so loading the first month's
   1,773 retired plants and 531 planned retirements writes **zero** events; a status-map
   correction is restated by the runner before the diff (lane FX1) and never reaches here.

   `before` / `after` carry the units, their technology, nameplate MW and dates, so the page can say
   "Unit 3 (650 MW, coal): December 2028 -> June 2030". A date in `before` comes from the diff and
   is printed at month precision; a year-only EIA date therefore reads as January of that year.
   `idempotency_key` = source, plant, event type, observation time and a hash of the units, so
   re-loading a run is a no-op. Events are public at load time, like every other change event
   (owner, 2026-09-21), and are served on the asset's own page; `event_visibility_filter` keeps
   them off the global feed, alerts and webhooks, which know proposal and opportunity subjects
   only (open item in docs/27 section R1).

Reached through `services/ingest/loader.py::load_from_files` (`SPECIALISED_LOADERS`), so the
scheduler's ordinary `load_source` job loads it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import pathlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import Registry
from pipeline.connectors.store import Store
from pipeline.context.retirements import GeneratorRecord, PlantRetirement, summarise_plants
from services.db.models import ASSET_EVENT_TYPES, Asset, Event, Source, SourceRun
from services.ingest.loader import GateRefused, _run_for_load, load_refusal, upsert_licence_and_source

log = logging.getLogger("services.ingest.retirements")

SOURCE_ID = "us.eia.860m.retirements"
#: The source the `power_plant` assets are keyed on (`pipeline/context/eia_plants.py`).
ASSET_SOURCE_ID = "us.eia.860m"


@dataclass
class RetirementLoadResult:
    source_id: str = SOURCE_ID
    generators: int = 0
    plants: int = 0
    assets_updated: int = 0
    assets_unchanged: int = 0
    plants_without_asset: int = 0
    #: Generator-level diff rows read, by `pipeline/diff.py` type.
    diff_rows: dict[str, int] = field(default_factory=dict)
    #: Asset events written this load, by type.
    events_created: dict[str, int] = field(default_factory=dict)
    events_skipped_idempotent: int = 0
    events_without_asset: int = 0
    #: True when the run had nothing to diff against: no events by construction.
    first_load: bool = False
    source_run_id: str | None = None


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ------------------------------------------------------------------------------------ frame -> units
def _identifiers(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        loaded = json.loads(str(value)) if value is not None else {}
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _float(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


def generator_records(records_df: pd.DataFrame) -> tuple[list[GeneratorRecord], dict[str, dict[str, Any]]]:
    """`GeneratorRecord`s for the plant summary, plus `{record_id: unit detail}` for the events."""
    out: list[GeneratorRecord] = []
    units: dict[str, dict[str, Any]] = {}
    for row in records_df.to_dict("records"):
        ids = _identifiers(row.get("identifiers"))
        plant_id = str(ids.get("eia_plant_id") or "")
        generator_id = str(ids.get("eia_generator_id") or "")
        if not plant_id:
            continue
        rec = GeneratorRecord(
            plant_id=plant_id,
            generator_id=generator_id,
            state=str(row.get("lifecycle_state") or "unknown"),
            capacity_mw=_float(row.get("capacity_mw")),
            technology=ids.get("technology"),
            date=ids.get("retirement"),
        )
        out.append(rec)
        units[str(row["record_id"])] = {
            "plant_id": plant_id,
            "generator_id": generator_id,
            "technology": rec.technology,
            "capacity_mw": rec.capacity_mw,
            "state": rec.state,
            "date": rec.date,
        }
    return out, units


def _as_of(records_df: pd.DataFrame) -> str | None:
    """The workbook's "as of" month, carried on every row's `raw` by the connector's parser."""
    for payload in records_df["raw"].head(5).tolist() if "raw" in records_df.columns else []:
        try:
            value = json.loads(str(payload)).get("_as_of")
        except (TypeError, ValueError, AttributeError):
            continue
        if value:
            return str(value)
    return None


# ------------------------------------------------------------------------------------------ assets
def _apply_to_asset(asset: Asset, summary: PlantRetirement, now: dt.datetime) -> bool:
    attributes = dict(asset.attributes or {})
    if summary.block is not None:
        attributes["retirement"] = summary.block
    else:
        attributes.pop("retirement", None)
    if (
        asset.status == summary.status
        and asset.retirement_year == summary.retirement_year
        and (asset.attributes or {}) == attributes
    ):
        return False
    asset.status = summary.status
    asset.retirement_year = summary.retirement_year
    asset.attributes = attributes
    asset.last_changed = now
    return True


# ------------------------------------------------------------------------------------------ events
def _month(value: Any) -> str | None:
    """A diff date (`YYYY-MM-DD`) as `YYYY-MM`; None stays None."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value)
    return text[:7] if len(text) >= 7 else text


def classify(
    diff_rows: list[dict[str, Any]], unit: dict[str, Any], observed_year: int
) -> tuple[str, str | None, str | None] | None:
    """`(asset event type, date before, date after)` for one generator's diff rows, or None when
    the change is not retirement news (module docstring table)."""
    by_type = {str(r["event_type"]): r for r in diff_rows}
    after_date = unit.get("date")
    if "status_change" in by_type:
        r = by_type["status_change"]
        before, after = str(r.get("before") or ""), str(r.get("after") or "")
        cod = by_type.get("cod_change")
        before_date = _month(cod.get("before")) if cod else None
        if after == "retired" and before != "retired":
            return "retired", before_date, after_date
        if before == "retired" and after != "retired":
            return "returned_to_service", before_date, None
        if after == "retiring" and before != "retiring":
            return "retirement_planned", None, after_date
        if before == "retiring" and after != "retiring":
            return "retirement_cancelled", before_date, None
        return None
    if "cod_change" in by_type and unit.get("state") == "retiring":
        r = by_type["cod_change"]
        if r.get("before") is not None and r.get("after") is not None and not pd.isna(r.get("before")):
            return "retirement_date_changed", _month(r.get("before")), after_date
        return None
    if "new" in by_type:
        if unit.get("state") == "retiring":
            return "retirement_planned", None, after_date
        if unit.get("state") == "retired" and after_date and int(str(after_date)[:4]) >= observed_year - 1:
            return "retired", None, after_date
    return None


_EVENT_WORDS = {
    "retirement_planned": "Retirement planned",
    "retirement_date_changed": "Planned retirement date moved",
    "retirement_cancelled": "Planned retirement withdrawn",
    "retired": "Retired",
    "returned_to_service": "Returned to service",
}


def _reason(event_type: str, units: list[dict[str, Any]]) -> str:
    mw = sum(u.get("capacity_mw") or 0.0 for u in units)
    n = len(units)
    head = f"{_EVENT_WORDS[event_type]}: {n} unit{'s' if n != 1 else ''}, {mw:,.1f} MW nameplate"
    moves = [
        f"unit {u['generator_id']} {u.get('date_before') or '?'} -> {u.get('date_after') or '?'}"
        for u in units
        if u.get("date_before") or u.get("date_after")
    ]
    return head + (" (" + "; ".join(moves) + ")" if moves else "") + ". Source: EIA-860M."


def _unit_hash(units: list[dict[str, Any]]) -> str:
    text = json.dumps(
        sorted((u["generator_id"], u.get("date_before"), u.get("date_after")) for u in units), default=str
    )
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]  # noqa: S324 -- identity key, not security


def _existing_keys(session: Session, keys: list[str]) -> set[str]:
    if not keys:
        return set()
    return set(session.scalars(select(Event.idempotency_key).where(Event.idempotency_key.in_(keys))).all())


def load_retirement_frames(
    session: Session,
    source: Source,
    records_df: pd.DataFrame,
    events_df: pd.DataFrame | None,
    *,
    run: SourceRun | None = None,
    now: dt.datetime | None = None,
) -> RetirementLoadResult:
    """Steps 1 and 2 of the module docstring over one run's frames. Commits nothing; the caller's
    transaction (the scheduler's `session_scope`, or the CLI) does."""
    now = now or utcnow()
    result = RetirementLoadResult(source_id=source.id)
    records, units = generator_records(records_df)
    result.generators = len(records)
    summaries = summarise_plants(records, as_of=_as_of(records_df))
    result.plants = len(summaries)

    assets: dict[str, Asset] = {
        a.source_asset_id: a
        for a in session.scalars(
            select(Asset).where(Asset.asset_type == "power_plant", Asset.source_id == ASSET_SOURCE_ID)
        )
    }
    for plant_id, summary in summaries.items():
        asset = assets.get(plant_id)
        if asset is None:
            result.plants_without_asset += 1
            continue
        if _apply_to_asset(asset, summary, now):
            result.assets_updated += 1
        else:
            result.assets_unchanged += 1
    session.flush()

    if events_df is None or not len(events_df):
        result.first_load = events_df is None
        return result
    result.diff_rows = {str(k): int(v) for k, v in events_df["event_type"].astype(str).value_counts().items()}

    by_record: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in events_df.to_dict("records"):
        by_record[str(ev["record_id"])].append(ev)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    observed: dict[tuple[str, str], str] = {}
    for record_id, rows in by_record.items():
        unit = units.get(record_id)
        if unit is None:
            continue  # a removed generator: not retirement news
        observed_at = str(rows[0].get("observed_at") or now.isoformat())
        try:
            observed_year = pd.Timestamp(observed_at).year
        except (TypeError, ValueError):
            observed_year = now.year
        kind = classify(rows, unit, observed_year)
        if kind is None:
            continue
        event_type, date_before, date_after = kind
        key = (unit["plant_id"], event_type)
        grouped[key].append(
            {
                "generator_id": unit["generator_id"],
                "technology": unit["technology"],
                "capacity_mw": unit["capacity_mw"],
                "date_before": date_before,
                "date_after": date_after,
            }
        )
        observed[key] = observed_at

    planned_keys: dict[tuple[str, str], str] = {}
    for (plant_id, event_type), unit_rows in grouped.items():
        when = observed[(plant_id, event_type)]
        planned_keys[(plant_id, event_type)] = (
            f"{source.id}:asset:{plant_id}:{event_type}:{when}:{_unit_hash(unit_rows)}"
        )
    existing = _existing_keys(session, list(planned_keys.values()))
    created: dict[str, int] = defaultdict(int)
    source_url = str(records_df["source_url"].iloc[0]) if len(records_df) else source.url
    for (plant_id, event_type), unit_rows in sorted(grouped.items()):
        asset = assets.get(plant_id)
        if asset is None:
            result.events_without_asset += 1
            continue
        idem_key = planned_keys[(plant_id, event_type)]
        if idem_key in existing:
            result.events_skipped_idempotent += 1
            continue
        unit_rows.sort(key=lambda u: str(u["generator_id"]))
        observed_ts: dt.datetime = pd.Timestamp(observed[(plant_id, event_type)]).to_pydatetime()
        if observed_ts.tzinfo is None:
            observed_ts = observed_ts.replace(tzinfo=dt.UTC)
        mw = round(sum(u.get("capacity_mw") or 0.0 for u in unit_rows), 3)
        plant_summary = summaries.get(plant_id)
        event = Event(
            subject_type="asset",
            subject_id=asset.id,
            event_type=event_type,
            observed_at=observed_ts,
            published_at=now,
            public_at=now,
            source_id=source.id,
            source_url=source_url,
            retrieved_at=observed_ts,
            licence_id=source.licence_id,
            before={
                "units": [{"generator_id": u["generator_id"], "date": u["date_before"]} for u in unit_rows]
            },
            after={
                "units": unit_rows,
                "capacity_mw": mw,
                "plant_status": plant_summary.status if plant_summary else None,
                "retirement_year": plant_summary.retirement_year if plant_summary else None,
            },
            changed_keys=["retirement"],
            actor_type="pipeline",
            reason=_reason(event_type, unit_rows),
            run_id=run.id if run is not None else None,
            idempotency_key=idem_key,
        )
        session.add(event)
        # One flush per event: `Event.seq` is assigned from MAX(seq) at insert time
        # (services/db/models.py), so a batch would collide.
        session.flush()
        created[event_type] += 1
    result.events_created = dict(created)
    return result


def load_retirement_run(
    session: Session,
    source_id: str,
    ts: str,
    *,
    data_root: pathlib.Path = pathlib.Path("data"),
    registry: Registry | None = None,
    store: Store | None = None,
    kind: Any = None,
) -> RetirementLoadResult:
    """`load_from_files`'s signature (the scheduler's `load_source` job calls it through
    `services/ingest/loader.py::SPECIALISED_LOADERS`): read the run's normalised frame, events and
    run record from the store, refuse a gated source, attach to the run's `source_run` row."""
    registry = registry or Registry()
    entry = registry.get(source_id)
    refusal = load_refusal(entry)
    if refusal:
        raise GateRefused(refusal)
    store = store if store is not None else Store(data_root)
    normalized_path = store.normalized_path(source_id, ts)
    if not store.exists(normalized_path):
        raise FileNotFoundError(store.locate(normalized_path))
    records_df = store.read_parquet(normalized_path)
    events_path = store.events_path(source_id, ts)
    events_df = store.read_parquet(events_path) if store.exists(events_path) else None
    run_path = store.run_path(source_id, ts)
    run_record = store.read_json(run_path) if store.exists(run_path) else None
    source = upsert_licence_and_source(session, entry, registry.version)
    run = _run_for_load(session, source, run_record, records_df, events_df, entry.egress)
    if (
        events_df is None
        and run_record is not None
        and (run_record.get("snapshot") or {}).get("previous_run_id")
    ):
        # A run with a predecessor and no events file simply had no changes; it is not a first load.
        events_df = pd.DataFrame(columns=["event_type", "record_id"])
    result = load_retirement_frames(session, source, records_df, events_df, run=run)
    result.source_run_id = str(run.id)
    log.info(
        "retirements loaded",
        extra={
            "source_id": source_id,
            "ts": ts,
            "assets_updated": result.assets_updated,
            "events": sum(result.events_created.values()),
        },
    )
    return result


__all__ = [
    "ASSET_EVENT_TYPES",
    "RetirementLoadResult",
    "classify",
    "generator_records",
    "load_retirement_frames",
    "load_retirement_run",
]
