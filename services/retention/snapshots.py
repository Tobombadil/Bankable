"""Raw-snapshot compaction: DA-10's "raw snapshots 24 months then monthly samples" (docs/04 DA-10;
docs/20 §3.2 **[A-7]**; docs/21 §4.3).

What it reads. The connector store (`pipeline/connectors/store.py`) keeps raw bytes at
`snapshots/{source_id}/{ts}.{ext}` and, for gated sources, `quarantine/snapshots/{source_id}/...`,
with one run record per run at `runs/{source_id}/{ts}.json` (the commit marker). `ts` is the fetch
time as `%Y%m%dT%H%M%SZ` (`ts_token`), so a snapshot's age is read from its name, never from a file
system's modification time. The context builders (`pipeline/context/`) store their downloads under
the same `snapshots/` tree and are covered by the same rule. A snapshot's run record is the one
under the same `ts`, unless that record names another object (a reparse or a re-checked hold
reuses an older object and writes none of its own). The connector runner's records name the object
(`snapshot.object_key`); several builders' records name none (EIA-923, EIA-860, LBNL) or give a
`snapshot.path`, and some carry no `status` (the EIA atlas layers), all measured on the operator's
data root on 2026-10-10.

The rule, per medium, tree and source:

* a snapshot whose name carries no `ts` is kept (`undated`): its age cannot be shown;
* a snapshot younger than 24 calendar months is kept (`young`);
* of the older ones, one per calendar month (UTC) and artefact is kept as that month's sample
  (`sample`): the newest one whose run was promoted (run record `status = ok`, a released hold
  included), or, in a month with no promoted run, the newest one. The newest promoted snapshot of a
  month is the bytes the platform's published output stood on at the end of that month, which is
  what a licence dispute asks about ("what did you publish, from what, and when"; docs/20 §12), and
  the next month's first diff was taken against it. The artefact is the fetched URL's host and path
  (query dropped), so a source that stores several different files per run keeps a sample of each:
  the EIA-923 builder stores the current and the previous year's workbooks (f923_2026.zip,
  f923_2025.zip) on every build, and one sample per source would drop one of them from every month.
  A snapshot with no URL on record is grouped by its extension. The cost of this reading: a source
  whose file name carries its release date (NESO's TEC register, `tec-register-10-october-2026.csv`)
  keeps every release (`docs/63` OP-8);
* every other older snapshot is deleted, unless the pipeline still reads it (`in_use`, `latest`):
  - the snapshot whose SHA-256 the next run compares against (`Store.last_snapshot`: the newest run
    `ok`, `unchanged` or `partial` with a recorded hash) and the one under the last promoted output,
    which a parser change restates and `run --reparse` re-reads (`Store.snapshot_bytes`;
    docs/20 §3.2). For each, the copy `Store.snapshot_bytes` resolves to is kept: the newest stored
    object with that hash written at or before the run that refers to it. Older identical copies are
    not read and follow the monthly rule (the builders store unchanged bytes again on every build,
    `docs/64` §7);
  - the newest snapshot of each extension (what a builder's `--latest-snapshot` reads,
    `pipeline/context/fuels.py` `latest_snapshot`) and of each artefact.

`snapshot` rows are never deleted (DA-10 "`snapshot` rows forever"). Their `retention_class`
(`full | sampled | expired`, docs/21 §4.3) is set from what is left: `sampled` when only snapshots
older than 24 months still hold the row's bytes, `expired` when every stored copy was older than 24
months and none is left, `full` when a younger copy exists. The class is derived from the state of
the medium, not from this run's deletions, so a run whose commit failed after its deletes is
corrected by the next one.

Media. The connector data root always (local files: the context builders write there whatever
`SNAPSHOT_STORE` says), plus the S3-compatible bucket when `SNAPSHOT_STORE=s3`
(`pipeline/connectors/objectstore.py`; both backends implement `list` and `delete`). On the local
root the sources are the directories on disk; a bucket listing has no directories
(`S3Backend.list` returns objects only), so there the sources are the registry's ids and the store's
`source` rows, and a prefix under an id in neither is not visited (kept).
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import logging
import pathlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from pipeline.connectors.objectstore import (
    LocalBackend,
    ObjectBackend,
    StoreError,
    backend_from_env,
    store_mode,
)
from pipeline.connectors.store import QuarantineStore, Store

logger = logging.getLogger("services.retention")

#: `pipeline.connectors.store.ts_token`'s format: the fetch time a snapshot's name carries.
TS_FORMAT = "%Y%m%dT%H%M%SZ"
#: DA-10 / docs/20 §3.2 [A-7]: raw bytes are kept whole for this many calendar months.
RAW_SNAPSHOT_MONTHS = 24
#: docs/21 §4.3 `snapshot.retention_class`.
RETENTION_CLASSES = ("full", "sampled", "expired")
#: Run statuses whose snapshot the next run is compared against (`Store._last_snapshot_entry`).
BASELINE_STATUSES = frozenset({"ok", "unchanged", "partial"})
#: A promoted run: its output reached `normalized/` (a passing run, or a released hold).
PROMOTED_STATUS = "ok"

#: Why a snapshot is kept, in the order the reasons are tested; `delete` is the only other outcome.
KEEP_REASONS = ("undated", "young", "latest", "in_use", "sample")


def months_before(at: dt.datetime, months: int) -> dt.datetime:
    """`at` minus whole calendar months, the day clamped to the target month's length
    (2026-03-31 minus one month is 2026-02-28)."""
    total = at.year * 12 + (at.month - 1) - months
    year, month0 = divmod(total, 12)
    day = min(at.day, calendar.monthrange(year, month0 + 1)[1])
    return at.replace(year=year, month=month0 + 1, day=day)


def taken_at(name: str) -> dt.datetime | None:
    """The fetch time in a snapshot's file name (`20260912T060000Z.xlsx`), or None."""
    try:
        return dt.datetime.strptime(name.split(".", 1)[0], TS_FORMAT).replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def artefact_of(url: str | None) -> str:
    """The artefact a fetched URL names: host and path, query and fragment dropped, so an API polled
    with a new window each run, or ERCOT's `mirDownload?doclookupId=...`, is one artefact."""
    if not url:
        return ""
    parts = urlsplit(str(url))
    return f"{parts.netloc}{parts.path}"


@dataclass(frozen=True)
class SnapshotFile:
    key: str
    name: str
    at: dt.datetime | None
    #: From the run record under the same `ts` (module docstring); None when no record claims the
    #: object (a run that crashed before its record, a hand-placed file, a builder that writes none).
    sha256: str | None = None
    status: str | None = None
    #: `artefact_of(snapshot.fetched_url)`; empty when no URL is on record.
    artefact: str = ""

    @property
    def ext(self) -> str:
        return self.name.split(".", 1)[1] if "." in self.name else ""

    @property
    def group(self) -> str:
        """The sampling unit within a source: the artefact, else the extension."""
        return self.artefact or f"*.{self.ext}"


def plan(files: Sequence[SnapshotFile], *, cutoff: dt.datetime, in_use: Iterable[str]) -> dict[str, str]:
    """`key -> reason` for every file: one of `KEEP_REASONS`, or `delete`. `in_use` holds the keys
    the pipeline still reads (`in_use_keys`). Pure: no I/O."""
    protected = set(in_use)
    outcome: dict[str, str] = {}
    dated = sorted(((f.at, f) for f in files if f.at is not None), key=lambda p: (p[0], p[1].name))
    newest: dict[str, str] = {}  # ascending, so the last one per extension and per artefact wins
    by_month: dict[tuple[int, int, str], list[SnapshotFile]] = {}
    for at, f in dated:
        newest[f"ext:{f.ext}"] = f.key
        if f.artefact:
            newest[f"url:{f.artefact}"] = f.key
        if at < cutoff:
            by_month.setdefault((at.year, at.month, f.group), []).append(f)
    latest = set(newest.values())
    samples = set()
    for month_files in by_month.values():
        promoted = [f for f in month_files if f.status == PROMOTED_STATUS]
        samples.add((promoted or month_files)[-1].key)
    for f in files:
        if f.at is None:
            outcome[f.key] = "undated"
        elif f.at >= cutoff:
            outcome[f.key] = "young"
        elif f.key in latest:
            outcome[f.key] = "latest"
        elif f.key in protected:
            outcome[f.key] = "in_use"
        elif f.key in samples:
            outcome[f.key] = "sample"
        else:
            outcome[f.key] = "delete"
    return outcome


def row_classes(
    recorded: Mapping[str, tuple[dt.datetime | None, str]],
    survivors: Iterable[SnapshotFile],
    *,
    cutoff: dt.datetime,
) -> dict[str, str]:
    """`sha256 -> retention_class` for the hashes this source's run records say were stored
    (`recorded`: file name -> (fetch time, hash)), from what is left on the medium (module
    docstring). A hash with a young or undated copy left is `full`; with only old copies left,
    `sampled`; with none left and every recorded copy older than the cutoff, `expired`. A hash whose
    young copy is missing for some other reason gets no class: this job does not explain it."""
    young: set[str] = set()
    old: set[str] = set()
    for f in survivors:
        if f.sha256 is None:
            continue
        (old if f.at is not None and f.at < cutoff else young).add(f.sha256)
    copies: dict[str, list[dt.datetime | None]] = {}
    for at, sha in recorded.values():
        copies.setdefault(sha, []).append(at)
    out: dict[str, str] = {}
    for sha, times in copies.items():
        if sha in young:
            out[sha] = "full"
        elif sha in old:
            out[sha] = "sampled"
        elif all(at is not None and at < cutoff for at in times):
            out[sha] = "expired"
    return out


def run_entries(store: Store, source_id: str) -> list[tuple[str, dict[str, Any]]]:
    """`(ts, record)` per readable run record of `source_id`, oldest first — what
    `Store._run_entries` reads, through the store's public backend."""
    prefix = store.key(store.root / "runs" / source_id) + "/"
    out: list[tuple[str, dict[str, Any]]] = []
    for key in store.backend.list(prefix):
        if not key.endswith(".json"):
            continue
        try:
            record = json.loads(store.backend.get(key).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, FileNotFoundError):
            continue
        if isinstance(record, dict):
            out.append((key.rsplit("/", 1)[-1][: -len(".json")], record))
    return out


def _claimed_name(record: Mapping[str, Any]) -> str | None:
    """The object a run record names (`snapshot.object_key` from the runner, `snapshot.path` from
    some builders), as a file name; None when it names none."""
    snap = record.get("snapshot") or {}
    key = snap.get("object_key") or snap.get("path")
    return str(key).replace("\\", "/").rsplit("/", 1)[-1] if key else None


def in_use_keys(
    store: Store,
    source_id: str,
    entries: Sequence[tuple[str, dict[str, Any]]],
    files: Sequence[SnapshotFile],
) -> set[str]:
    """The objects the pipeline still reads (module docstring): for the next run's comparison
    baseline and for the last promoted run, the newest stored copy of its hash at or before that
    run, which is the one `Store.snapshot_bytes` returns. When no dated copy qualifies, every copy
    of the hash is kept rather than guessing."""
    refs: list[tuple[str, str]] = []
    for ts, record in reversed(entries):
        sha = (record.get("snapshot") or {}).get("sha256")
        if sha and record.get("status") in BASELINE_STATUSES:
            refs.append((str(sha), ts))
            break
    promoted = store.last_promoted(source_id)
    if promoted is not None and (sha := (promoted[1].get("snapshot") or {}).get("sha256")):
        refs.append((str(sha), promoted[0]))
    out: set[str] = set()
    for sha, ts in refs:
        copies = [f for f in files if f.sha256 == sha]
        earlier = [f for f in copies if f.at is not None and f.name.split(".", 1)[0] <= ts]
        if earlier:
            out.add(max(earlier, key=lambda f: f.name).key)
        else:
            out.update(f.key for f in copies)
    return out


@dataclass
class SourceOutcome:
    medium: str
    tree: str
    source_id: str
    reasons: dict[str, int] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    classes: dict[str, str] = field(default_factory=dict)

    @property
    def to_delete(self) -> int:
        return self.reasons.get("delete", 0)


def compact_source(
    store: Store, source_id: str, *, medium: str, cutoff: dt.datetime, dry_run: bool
) -> SourceOutcome | None:
    """Apply the rule to one source in one store tree. None when the source has no snapshot here."""
    keys = store.backend.list(store.key(store.root / "snapshots" / source_id) + "/")
    if not keys:
        return None
    entries = run_entries(store, source_id)
    by_ts = dict(entries)
    files = []
    for key in keys:
        name = key.rsplit("/", 1)[-1]
        record = by_ts.get(name.split(".", 1)[0])
        claimed = _claimed_name(record) if record is not None else None
        # The record under the same `ts` describes this object unless it names another one (module
        # docstring); a record that names none is a builder's.
        snap = (record.get("snapshot") or {}) if record is not None and claimed in (None, name) else {}
        files.append(
            SnapshotFile(
                key=key,
                name=name,
                at=taken_at(name),
                sha256=str(snap["sha256"]) if snap.get("sha256") else None,
                status=record.get("status") if snap and record is not None else None,
                artefact=artefact_of(snap.get("fetched_url")),
            )
        )
    decision = plan(files, cutoff=cutoff, in_use=in_use_keys(store, source_id, entries, files))
    tree = "quarantine" if isinstance(store, QuarantineStore) else "snapshots"
    outcome = SourceOutcome(medium=medium, tree=tree, source_id=source_id)
    for reason in decision.values():
        outcome.reasons[reason] = outcome.reasons.get(reason, 0) + 1
    gone: set[str] = set()
    for key, reason in decision.items():
        if reason != "delete":
            continue
        if dry_run:
            gone.add(key)
            continue
        try:
            store.backend.delete(key)
        except (StoreError, OSError) as exc:
            outcome.errors.append(f"{medium}:{key}: {exc.__class__.__name__}")
            continue
        gone.add(key)
        outcome.deleted.append(key)
    survivors = [f for f in files if f.key not in gone]
    # Every copy on record: the files found this run, and the objects earlier runs' records name,
    # so a hash whose last copy an earlier run deleted still reads `expired`.
    recorded: dict[str, tuple[dt.datetime | None, str]] = {}
    for f in files:
        if f.sha256:
            recorded[f.name] = (f.at, f.sha256)
    for ts, record in entries:
        named, sha = _claimed_name(record), (record.get("snapshot") or {}).get("sha256")
        if named and sha and named.split(".", 1)[0] == ts and named not in recorded:
            recorded[named] = (taken_at(named), str(sha))
    outcome.classes = row_classes(recorded, survivors, cutoff=cutoff)
    if outcome.to_delete:
        logger.info(
            "retention: %s %s/%s: %s of %s raw snapshots %s",
            medium,
            tree,
            source_id,
            outcome.to_delete,
            len(files),
            "would be deleted (dry run)" if dry_run else "deleted",
            extra={"source_id": source_id, "medium": medium, "tree": tree, "count": outcome.to_delete},
        )
    return outcome


def default_media(
    data_root: pathlib.Path, env: Mapping[str, str] | None = None
) -> list[tuple[str, ObjectBackend]]:
    """The local data root always; the bucket too when `SNAPSHOT_STORE=s3` (module docstring).
    Raises `StoreConfigError` when `s3` is selected without its settings, never falling back."""
    media: list[tuple[str, ObjectBackend]] = [("local", LocalBackend(data_root))]
    if store_mode(env) == "s3":
        media.append(("s3", backend_from_env(data_root, env)))
    return media


def _local_source_dirs(store: Store, backend: LocalBackend) -> list[str]:
    """The source directories under this tree's `snapshots/` on the backend's own root."""
    directory = backend.root / store.key(store.root / "snapshots")
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.is_dir() and not p.name.startswith("."))


def compact_snapshots(
    data_root: pathlib.Path,
    *,
    cutoff: dt.datetime,
    dry_run: bool,
    media: Sequence[tuple[str, ObjectBackend]],
    source_ids: Iterable[str],
) -> list[SourceOutcome]:
    """Every medium, both trees (`snapshots/` and `quarantine/snapshots/`), every source found."""
    candidates = sorted(set(source_ids))
    outcomes: list[SourceOutcome] = []
    for medium, backend in media:
        for store in (Store(data_root, backend=backend), QuarantineStore(data_root, backend=backend)):
            ids = _local_source_dirs(store, backend) if isinstance(backend, LocalBackend) else candidates
            for source_id in ids:
                try:
                    result = compact_source(store, source_id, medium=medium, cutoff=cutoff, dry_run=dry_run)
                except StoreError as exc:
                    # A listing or a run record that cannot be read: nothing is deleted for this
                    # source (deletes are the last step), the error is reported, the rest go on.
                    tree = "quarantine" if isinstance(store, QuarantineStore) else "snapshots"
                    result = SourceOutcome(medium=medium, tree=tree, source_id=source_id)
                    result.errors.append(f"{medium}:{tree}/{source_id}: {exc.__class__.__name__}")
                if result is not None:
                    outcomes.append(result)
    return outcomes


__all__ = [
    "KEEP_REASONS",
    "RAW_SNAPSHOT_MONTHS",
    "RETENTION_CLASSES",
    "SnapshotFile",
    "SourceOutcome",
    "artefact_of",
    "compact_snapshots",
    "compact_source",
    "default_media",
    "months_before",
    "plan",
    "row_classes",
    "taken_at",
]
