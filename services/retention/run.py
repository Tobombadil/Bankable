"""The data-retention job (docs/04 DA-10): one entry point that applies every DA-10 rule whose data
exists, a report of counts per rule, and one run-log row per run.

DA-10, verbatim: "Raw snapshots 24 months then monthly samples, `snapshot` rows forever (`docs/20`
§3.2, A-7); documents per their licence flags; `model_call` prompts 90 days, outputs kept; sessions
30 days; alerts 12 months (`docs/20` §11); retention runs as a scheduled job with its own run log."

Each rule as applied (`RULES` holds the text each one follows; ages are measured back from `now`):

* **sessions** — a `session` row (docs/21 §4.4) is deleted once it is 30 days old (`created_at`
  before `now - 30 days`) and no longer valid (`expires_at <= now`). Today a session's
  `expires_at` is `created_at + 30 days` (`services/api/auth.py` `_SESSION_IDLE_DAYS`), so the
  second test never holds a row back; it is there so that a later change to the lifetime can never
  make this job sign someone out. Such rows are counted (`kept_unexpired`), not deleted. Nothing
  references a session row.
* **alerts** — docs/20 §11 "alerts 12 months" is the `alert` table, the delivery log (one row per
  digest or webhook delivery), not `saved_search`, which is the subscription: docs/21 §3.16 says of
  `alert` "Retention 12 months (`docs/20` §11), then rows are aggregated into counts and the
  recipient column is dropped". Applied: `recipient` (the address at send time, the row's only
  personal datum besides the opaque `user_id`) is set to null on rows created more than 12 calendar
  months ago. Not applied: "aggregated into counts". §3.16 names no table for the counts and does
  not say the rows go; the rows stay, so the counts can be taken from them at any time. An
  unsubscribe link in a digest older than 12 months still pauses the email channel
  (`services/api/unsubscribe_routes.py`); with the address gone it can no longer add a
  suppression hash, which `suppress` already treats as nothing to do.
* **raw snapshots** and **`snapshot` rows forever** — `services.retention.snapshots` (its
  docstring has the rule, the monthly sample and what is never deleted). No `snapshot` row is
  deleted; `retention_class` follows the object.
* **`model_call` prompts 90 days, outputs kept** — skipped: the table stores neither (its columns
  are the docs/21 §4.4 list: purpose, alias, template id and version, token counts, cost, latency,
  error) and has no writer (`services/modelgw` does not exist). There is nothing to delete; the
  rule applies the day a gateway stores prompt text.
* **documents per their licence flags** — skipped: no code writes `document` rows
  (`services/api/documents.py`), and no licence flag on `licence` states a retention period, so the
  rule has neither data nor a period to apply.
* **`docs/13` §5.4 rule 7** (personal fields of withdrawn/cancelled projects purged at 24 months) is
  not DA-10's but is the retention rule DA-13 adopts; skipped: which fields are "personal fields",
  what the 24 months run from, and whether a purge redacts or deletes are not settled
  (`docs/13` §5.5.5 open item 4), and nothing reaches the age before 2028-09 (`docs/63` §5.2).

Run log (DA-10 "its own run log"). One append-only `event` row per run that is not a dry run, in
the same transaction as the rule's writes, the way `services/visibility_audit/run.py` persists the
M-11 audit: `subject_type = source` with a fixed pseudo-source subject (`__retention__`),
`event_type = retention_run`, `actor_type = system`, the report in `after`, never published.
Read back with `latest_runs` or `python -m services.retention.run --latest 5`. No admin route
reads it yet (`docs/63` §8).

Run by hand (`--dry-run` reports what would change and writes nothing, not even the run log):

    python -m services.retention.run [--dry-run] [--now 2026-10-10T01:47:00Z] [--data-root PATH] [--json]

Scheduled: `retention_tick` in `infra/scheduler/app.py`, daily (docs/60 §6.3), body
`infra/scheduler/jobs.py::retention_tick_job`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import sys
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from pipeline.connectors.objectstore import LocalBackend, ObjectBackend
from pipeline.connectors.registry import Registry
from services.db.models import Alert, Event, Snapshot, Source, UserSession
from services.db.session import get_engine, get_sessionmaker, session_scope
from services.ids import public_id
from services.retention.snapshots import (
    RAW_SNAPSHOT_MONTHS,
    SourceOutcome,
    compact_snapshots,
    default_media,
    months_before,
)

logger = logging.getLogger("services.retention")

#: The run-log row (module docstring). `event.event_type` carries no CHECK; the subject is a fixed
#: pseudo-source, hashed the way `services/api/admin_sources.py` hashes a source id.
EVENT_TYPE = "retention_run"
SUBJECT_TYPE = "source"
SUBJECT_ID = uuid.uuid5(uuid.NAMESPACE_URL, "bankable:source:__retention__")
RUN_REASON = "DA-10 data retention (docs/04 DA-10; docs/20 §3.2, §11; docs/21 §3.16, §4.3)"

SESSION_RETENTION = dt.timedelta(days=30)
ALERT_RETENTION_MONTHS = 12
#: Per-source detail kept in the run log is capped; the totals never are.
BY_SOURCE_CAP = 200

#: The text each rule follows, with where it is stated (module docstring).
RULES: dict[str, str] = {
    "sessions": "sessions 30 days (docs/04 DA-10; docs/20 §11)",
    "alerts": (
        "alerts 12 months (docs/04 DA-10; docs/20 §11): retention 12 months, then rows are aggregated "
        "into counts and the recipient column is dropped (docs/21 §3.16)"
    ),
    "raw_snapshots": (
        "raw snapshots 24 months then monthly samples (docs/04 DA-10; docs/20 §3.2 [A-7]; docs/21 §4.3)"
    ),
    "snapshot_rows": "`snapshot` rows forever (docs/04 DA-10; docs/21 §4.3)",
    "model_call_prompts": "`model_call` prompts 90 days, outputs kept (docs/04 DA-10; docs/20 §11)",
    "documents": "documents per their licence flags (docs/04 DA-10)",
    "withdrawn_personal_fields": (
        "withdrawn/cancelled projects have their personal fields purged at 24 months (docs/13 §5.4 rule 7)"
    ),
}

SKIPPED: dict[str, str] = {
    "model_call_prompts": (
        "no data: `model_call` stores no prompt and no output (docs/21 §4.4 columns) and has no writer "
        "(services/modelgw does not exist)"
    ),
    "documents": (
        "no data and no period: nothing writes `document` rows, and no licence flag states a retention period"
    ),
    "withdrawn_personal_fields": (
        "not settled: which fields, from which date, redact or delete (docs/13 §5.5.5 open item 4); "
        "nothing reaches the age before 2028-09"
    ),
}


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def _count(db: Session, model: type[Any], *where: Any) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def expire_sessions(db: Session, now: dt.datetime, *, dry_run: bool) -> dict[str, Any]:
    cutoff = now - SESSION_RETENTION
    past_age = UserSession.created_at < cutoff
    ended = UserSession.expires_at <= now
    matched = _count(db, UserSession, past_age, ended)
    kept_unexpired = _count(db, UserSession, past_age, ~ended)
    changed = 0
    if matched and not dry_run:
        result = db.execute(
            delete(UserSession).where(past_age, ended).execution_options(synchronize_session=False)
        )
        changed = int(getattr(result, "rowcount", 0) or 0)
    return {
        "status": "applied",
        "rule": RULES["sessions"],
        "cutoff": cutoff.isoformat(),
        "action": "delete the row",
        "matched": matched,
        "changed": changed,
        "kept_unexpired": kept_unexpired,
    }


def drop_alert_recipients(db: Session, now: dt.datetime, *, dry_run: bool) -> dict[str, Any]:
    cutoff = months_before(now, ALERT_RETENTION_MONTHS)
    where = (Alert.created_at < cutoff, Alert.recipient.is_not(None))
    matched = _count(db, Alert, *where)
    changed = 0
    if matched and not dry_run:
        result = db.execute(
            update(Alert).where(*where).values(recipient=None).execution_options(synchronize_session=False)
        )
        changed = int(getattr(result, "rowcount", 0) or 0)
    return {
        "status": "applied",
        "rule": RULES["alerts"],
        "cutoff": cutoff.isoformat(),
        "action": "set recipient to null",
        "not_applied": (
            "aggregation into counts: docs/21 §3.16 names no store for the counts and does not say the "
            "rows are deleted, so the rows stay"
        ),
        "matched": matched,
        "changed": changed,
    }


_AVAILABILITY = {"expired": 0, "sampled": 1, "full": 2}


def classify_snapshot_rows(
    db: Session, outcomes: Sequence[SourceOutcome], *, dry_run: bool
) -> dict[str, Any]:
    """Set `snapshot.retention_class` from what the compaction left (`row_classes`). Rows are matched
    by `(source_id, sha256)`, the table's unique key; a row whose hash no run record names is left
    as it is."""
    wanted: dict[tuple[str, str], str] = {}
    for outcome in outcomes:
        for sha, cls in outcome.classes.items():
            key = (outcome.source_id, sha)
            # One hash can be stored on two media or in both trees: the most available class wins,
            # so a row is `expired` only when every place that held its bytes has let them go.
            if key not in wanted or _AVAILABILITY[cls] > _AVAILABILITY[wanted[key]]:
                wanted[key] = cls
    by_class: dict[str, int] = {}
    matched = 0
    for source_id in sorted({s for s, _ in wanted}):
        shas = [sha for (s, sha) in wanted if s == source_id]
        for i in range(0, len(shas), 500):
            rows = db.scalars(
                select(Snapshot).where(
                    Snapshot.source_id == source_id, Snapshot.sha256.in_(shas[i : i + 500])
                )
            )
            for row in rows:
                cls = wanted[(source_id, row.sha256)]
                if row.retention_class == cls:
                    continue
                matched += 1
                by_class[cls] = by_class.get(cls, 0) + 1
                if not dry_run:
                    row.retention_class = cls
    if matched and not dry_run:
        db.flush()
    return {
        "status": "applied",
        "rule": RULES["snapshot_rows"],
        "action": "never delete; set retention_class to full | sampled | expired",
        "rows": _count(db, Snapshot),
        "matched": matched,
        "changed": 0 if dry_run else matched,
        "by_class": dict(sorted(by_class.items())),
    }


def _snapshot_report(
    outcomes: Sequence[SourceOutcome], *, media: Sequence[str], cutoff: dt.datetime, dry_run: bool
) -> dict[str, Any]:
    totals: dict[str, int] = {}
    errors: list[str] = []
    by_source: dict[str, dict[str, int]] = {}
    for o in outcomes:
        for reason, n in o.reasons.items():
            totals[reason] = totals.get(reason, 0) + n
        errors.extend(o.errors)
        has_old = any(reason not in ("young", "undated") for reason in o.reasons)
        if (has_old or o.errors) and len(by_source) < BY_SOURCE_CAP:
            label = f"{o.medium}:{o.tree}/{o.source_id}"
            by_source[label] = {k: v for k, v in sorted(o.reasons.items()) if k != "young"}
    matched = totals.get("delete", 0)
    return {
        "status": "applied",
        "rule": RULES["raw_snapshots"],
        "cutoff": cutoff.isoformat(),
        "action": "keep one sample per source per calendar month, delete the rest",
        "media": list(media),
        "sources": len(outcomes),
        "files": sum(totals.values()),
        "kept": {k: totals[k] for k in sorted(totals) if k != "delete"},
        "matched": matched,
        "changed": 0 if dry_run else sum(len(o.deleted) for o in outcomes),
        "errors": errors,
        "by_source": by_source,
    }


def _default_source_ids(db: Session) -> list[str]:
    """For a bucket, which has no directory listing (`snapshots` module docstring)."""
    ids = set(Registry().ids())
    ids.update(db.scalars(select(Source.id)))
    return sorted(ids)


def apply_retention(
    db: Session,
    *,
    now: dt.datetime,
    data_root: pathlib.Path,
    dry_run: bool = False,
    media: Sequence[tuple[str, ObjectBackend]] | None = None,
    source_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Apply every rule (module docstring) and return the report. Writes go through `db`; the caller
    commits (or `run_retention` does). With `dry_run`, nothing is written or deleted and `changed` is
    0 everywhere; `matched` says what a real run would change. `media` defaults to the local data
    root plus the bucket when `SNAPSHOT_STORE=s3`; `source_ids` (used for a bucket) to the registry's
    ids and the store's `source` rows."""
    at = _aware(now).astimezone(dt.UTC)
    chosen = list(media) if media is not None else default_media(pathlib.Path(data_root))
    raw_cutoff = months_before(at, RAW_SNAPSHOT_MONTHS)
    rules: dict[str, Any] = {
        "sessions": expire_sessions(db, at, dry_run=dry_run),
        "alerts": drop_alert_recipients(db, at, dry_run=dry_run),
    }
    if source_ids is not None:
        ids = list(source_ids)
    elif any(not isinstance(backend, LocalBackend) for _, backend in chosen):
        ids = _default_source_ids(db)
    else:
        ids = []  # a local root is enumerated from its directories
    outcomes = compact_snapshots(
        pathlib.Path(data_root), cutoff=raw_cutoff, dry_run=dry_run, media=chosen, source_ids=ids
    )
    rules["raw_snapshots"] = _snapshot_report(
        outcomes, media=[name for name, _ in chosen], cutoff=raw_cutoff, dry_run=dry_run
    )
    rules["snapshot_rows"] = classify_snapshot_rows(db, outcomes, dry_run=dry_run)
    for name, reason in SKIPPED.items():
        rules[name] = {"status": "skipped", "rule": RULES[name], "reason": reason}
    return {
        "run_id": str(uuid.uuid4()),
        "run_at": at.isoformat(),
        "dry_run": dry_run,
        "data_root": str(data_root),
        "rules": rules,
        "errors": len(rules["raw_snapshots"]["errors"]),
    }


# ================================================================================ run log
def persist_run(db: Session, report: dict[str, Any]) -> Event:
    """One `event` row per run (module docstring): system-actored, unpublished on every tier,
    idempotent per `run_id`."""
    event = Event(
        subject_type=SUBJECT_TYPE,
        subject_id=SUBJECT_ID,
        event_type=EVENT_TYPE,
        observed_at=dt.datetime.fromisoformat(str(report["run_at"])),
        published_at=None,
        public_at=None,
        before=None,
        after=report,
        changed_keys=sorted(report),
        actor_type="system",
        reason=RUN_REASON,
        job_id=f"{EVENT_TYPE}:{report['run_id']}",
        idempotency_key=f"retention:{SUBJECT_TYPE}:{SUBJECT_ID}:{EVENT_TYPE}:{report['run_id']}",
    )
    db.add(event)
    db.flush()
    return event


def latest_runs(db: Session, *, limit: int = 1) -> list[Event]:
    stmt = (
        select(Event)
        .where(Event.event_type == EVENT_TYPE, Event.subject_type == SUBJECT_TYPE)
        .order_by(Event.seq.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def run_retention(
    session_factory: sessionmaker[Session],
    *,
    now: dt.datetime | None = None,
    data_root: pathlib.Path | None = None,
    dry_run: bool = False,
    media: Sequence[tuple[str, ObjectBackend]] | None = None,
    source_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """`apply_retention` and, unless `dry_run`, the run-log row, in one transaction."""
    if data_root is None:
        from pipeline.connectors.store import DATA_DIR

        data_root = DATA_DIR
    with session_scope(session_factory) as db:
        report = apply_retention(
            db,
            now=now or dt.datetime.now(dt.UTC),
            data_root=data_root,
            dry_run=dry_run,
            media=media,
            source_ids=source_ids,
        )
        if not dry_run:
            event = persist_run(db, report)
            report["event_id"] = public_id("evt", event.id)
    return report


def summarise(report: dict[str, Any]) -> dict[str, Any]:
    """One flat line per run: `matched`/`changed` per rule, skipped rules by name, the error count."""
    out: dict[str, Any] = {
        "run_id": report["run_id"],
        "dry_run": report["dry_run"],
        "errors": report["errors"],
    }
    for name, rule in report["rules"].items():
        if rule["status"] == "skipped":
            out[f"{name}_skipped"] = True
        else:
            out[f"{name}_matched"] = rule["matched"]
            out[f"{name}_changed"] = rule["changed"]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m services.retention.run", description="DA-10 data retention."
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    parser.add_argument("--now", help="ISO time to measure ages from (default: now, UTC)")
    parser.add_argument(
        "--data-root", type=pathlib.Path, help="connector data root (default: INFRAQUE_DATA_DIR, else data/)"
    )
    parser.add_argument("--json", action="store_true", help="print the full report (default: summary)")
    parser.add_argument("--latest", type=int, metavar="N", help="print the last N run-log rows and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(message)s")
    factory = get_sessionmaker(get_engine(os.environ.get("DATABASE_URL")))
    if args.latest:
        with session_scope(factory) as db:
            rows = [e.after for e in latest_runs(db, limit=args.latest)]
        sys.stdout.write(json.dumps(rows, indent=2, default=str) + "\n")
        return 0
    now = dt.datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
    report = run_retention(factory, now=now, data_root=args.data_root, dry_run=args.dry_run)
    sys.stdout.write(json.dumps(report if args.json else summarise(report), indent=2, default=str) + "\n")
    return 1 if report["errors"] else 0


if __name__ == "__main__":  # pragma: no cover - exercised through `main()` in tests
    sys.exit(main())
