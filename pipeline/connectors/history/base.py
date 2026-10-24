"""The history connector contract (`docs/24-trends.md` §9).

A history connector turns an official monthly archive into one document per month
(`pipeline/connectors/history/documents.py`). It is the record connector's three pure steps on the
same parts (`PoliteSession`, the registry's `SourceEntry`, the snapshot `Store`), with a plan in
front because a backfill reads many files:

    plan(mode, held)  -> list[ArchiveItem]     which files this run needs, none it already holds
    fetch(item)       -> Iterator[MonthFile]   network or stored bytes; a yearly zip yields a month
                                               per member
    summarise(month)  -> dict                  parse and tally: the document's body; never the network

It is not a `pipeline.connectors.base.Connector`: that class types `kind` as proposal, opportunity
or document and its runner diffs records into lifecycle events. A month of an archive is context,
not news: the series show it, and nothing here becomes a public `event` (a source's first load is
not news, docs/00-PLAN.md 2026-10-23).

Every document carries the provenance quartet (`source_id`, `source_url`, `retrieved_at`,
`licence_id`) and a `raw` block naming the stored snapshot the figures were computed from
(CLAUDE.md). `source_url` is always the publisher's own address for that month's file, also when
the bytes came from a mirror or a person's download; `retrieved_from` says which route they took.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import io
import re
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from pipeline.connectors.base import ConnectorError, ParseError, _code_digest, utcnow
from pipeline.connectors.http import PoliteSession
from pipeline.connectors.registry import SourceEntry
from pipeline.connectors.store import Store

MODES: tuple[str, ...] = ("backfill", "monthly")
XLSX_MAGIC = b"PK"

#: How a month's bytes reached us, and which copy wins when two routes hold the same month: the
#: publisher's own server (`origin`, or `shared`: the bytes another connector fetched from it)
#: over a person's download (`inbox`) over a public mirror (`mirror`). `restate` re-reads a
#: stored snapshot under a changed parser and keeps the route the document already records.
ROUTE_RANK: dict[str, int] = {"origin": 3, "shared": 3, "inbox": 2, "mirror": 1}


class HistoryGateRefused(ConnectorError):
    """The source may not be fetched for a publication path."""


def gate(entry: SourceEntry, publishable: frozenset[str]) -> None:
    """CLAUDE.md: never fetch from sources marked `reuse: restricted` for publication paths; nor a
    row still `pending` or `excluded`, nor one that publishes nothing."""
    status = str(entry.raw.get("status") or "")
    if entry.never_ingest:
        raise HistoryGateRefused(f"{entry.id} is a private aggregator; it has no connector by design")
    if status in ("pending", "excluded"):
        raise HistoryGateRefused(f"{entry.id} is `status: {status}` in data/sources.yaml; nothing is fetched")
    if entry.reuse not in publishable:
        raise HistoryGateRefused(f"{entry.id} has reuse={entry.reuse!r}, not publishable; not fetched")
    if entry.publication == "none":
        raise HistoryGateRefused(f"{entry.id}: publication=none publishes nothing; not fetched")


@dataclass(frozen=True)
class HeldPeriod:
    """What the store already holds for one month: enough for a plan to skip or replace it."""

    period: str
    route: str
    sha256: str
    retrieved_at: str
    parser_version: str
    key: str = ""
    snapshot_key: str | None = None
    held: bool = False
    source_url: str = ""
    #: The publisher's own identifier of the file (ERCOT's DocID), where it has one.
    file_id: str | None = None
    #: A newer reading of the month its data-quality checks held, kept beside the served document
    #: until an operator releases it (`documents.MonthlyStore.write_pending`): its bytes, parser and
    #: file, so the same file is neither fetched nor parsed again.
    pending_sha256: str | None = None
    pending_parser_version: str | None = None
    pending_file_id: str | None = None


@dataclass(frozen=True)
class ArchiveItem:
    """One unit of work: a file to read (a month's workbook, a yearly zip, a report) and the months
    it is expected to hold (a hint; the file itself says which month it is)."""

    key: str
    route: str
    url: str
    periods: tuple[str, ...] = ()
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class MonthFile:
    """One month's file, ready to summarise."""

    content: bytes
    source_url: str
    retrieved_at: dt.datetime
    route: str
    item_key: str
    period_hint: str | None = None
    via: dict[str, Any] = field(default_factory=dict)
    #: The object key of a snapshot already stored for these bytes (shared or restated); None means
    #: the runner stores them.
    snapshot_key: str | None = None
    #: The publisher's own validators (`ETag`, `Last-Modified`) where the route had them.
    validators: dict[str, str] = field(default_factory=dict)
    #: The route the document records when this differs from `route` (a restated month keeps the
    #: route it first came by).
    record_route: str | None = None

    def __post_init__(self) -> None:
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware UTC")

    @property
    def retrieved_at_iso(self) -> str:
        return self.retrieved_at.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class HistoryConnector:
    """Base class. Subclasses set the class attributes and implement plan/fetch/summarise."""

    source_id: ClassVar[str]
    #: The register source whose stored snapshots this one reads (`shared` route): a restatement
    #: whose own snapshot is gone reads another stored copy with the same sha256 from there.
    shared_source_id: ClassVar[str | None] = None
    #: Bump on a deliberate change of what `summarise` produces; the runner re-reads every stored
    #: month whose document names another version (a restatement, never news).
    parser_version: ClassVar[str] = "1.0.0"
    honour_robots: ClassVar[bool] = True

    def __init__(
        self,
        source: SourceEntry,
        http: PoliteSession | None = None,
        *,
        store: Store,
        clock: Callable[[], dt.datetime] = utcnow,
        previous_runs: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        if source.id != self.source_id:
            raise ValueError(f"{type(self).__name__} is bound to {self.source_id}, got {source.id}")
        self.source = source
        self.store = store
        self.http = http or PoliteSession(rate_limits=self.rate_limits())
        self.clock = clock
        #: This source's earlier run records, oldest first (validators, processed inbox files).
        self.previous_runs = list(previous_runs)
        #: What `plan` was given, for `fetch` to skip months already held (the runner adds each
        #: month it writes, so a later item of the same run sees it).
        self._held: Mapping[str, HeldPeriod] = {}
        #: A route that could not be planned while the others could (the mirror's listing down):
        #: recorded on the run, which then ends `partial`.
        self.plan_errors: list[dict[str, Any]] = []

    def rate_limits(self) -> dict[str, float]:
        """Requests per second per host this connector asks of; the manifest's limit for its own
        host."""
        return {self.source.host: self.source.max_rps}

    def now(self) -> dt.datetime:
        return self.clock()

    def backfill_key(self) -> str | None:
        """What a backfill read beyond the standing routes (EIA's mirror record), recorded on the
        run: `auto` runs a backfill again when it changes (`runner.effective_mode`), so a mirror
        switched on, or moved to a newer record, is read without an operator's run."""
        return None

    # ---------------------------------------------------------------------------------- contract
    def plan(self, mode: str, held: Mapping[str, HeldPeriod]) -> list[ArchiveItem]:
        raise NotImplementedError

    def fetch(self, item: ArchiveItem) -> Iterator[MonthFile]:
        raise NotImplementedError

    def summarise(self, month: MonthFile) -> dict[str, Any]:
        raise NotImplementedError

    # ----------------------------------------------------------------------------------- helpers
    def restate_items(self, held: Mapping[str, HeldPeriod]) -> list[ArchiveItem]:
        """A month whose document another parser version wrote, re-read from its stored snapshot;
        not one whose restatement under this version is already held beside it."""
        out = []
        version = self.version()
        for period, h in sorted(held.items()):
            restated_and_held = h.pending_parser_version == version and h.pending_sha256 == h.sha256
            if h.parser_version != version and h.snapshot_key and not restated_and_held:
                out.append(
                    ArchiveItem(
                        key=f"restate:{period}",
                        route="restate",
                        url=h.snapshot_key,
                        periods=(period,),
                        meta={"route": h.route, "sha256": h.sha256, "item_key": h.key},
                    )
                )
        return out

    def fetch_restated(self, item: ArchiveItem, *, source_url: str, retrieved_at: str) -> MonthFile:
        """The month's stored bytes for a restatement: the snapshot its document names, or, when
        that object is gone or no longer matches its sha256 (a retention run, a restored volume),
        another stored copy with the same sha256 that `shared_source_id` holds. `ConnectorError`
        when neither is left: the connector may then fetch the file again (ERCOT, by its DocID)."""
        sha = str(item.meta.get("sha256") or "")
        key: str | None = item.url
        content: bytes | None = None
        error: BaseException | None = None
        try:
            content = self.store.backend.get(item.url)
        except Exception as exc:  # a gone object costs this month its restatement, nothing more
            error = exc
        if content is not None and sha and hashlib.sha256(content).hexdigest() != sha:
            content, error = None, ConnectorError(f"stored snapshot {item.url} fails its sha256")
        if content is None:
            key, content = self._stored_copy(sha)
        if content is None or key is None:
            raise ConnectorError(f"stored snapshot {item.url} unreadable and no other copy: {error!r}")
        return MonthFile(
            content=content,
            source_url=source_url,
            retrieved_at=parse_time(retrieved_at),
            route="restate",
            item_key=str(item.meta.get("item_key") or item.key),
            period_hint=item.periods[0] if item.periods else None,
            snapshot_key=key,
            record_route=str(item.meta.get("route") or "origin"),
            via={"restated_from": key},
        )

    def _stored_copy(self, sha: str) -> tuple[str | None, bytes | None]:
        """Another stored copy of the bytes with `sha` (`shared_source_id`'s snapshots), checked."""
        if not sha or not self.shared_source_id:
            return None, None
        for snap in stored_snapshots(self.store, self.shared_source_id):
            if snap.sha256 != sha:
                continue
            try:
                content = self.store.backend.get(snap.key)
            except Exception:  # noqa: S112 -- try the next copy; the caller reports none left
                continue
            if hashlib.sha256(content).hexdigest() == sha:
                return snap.key, content
        return None, None

    @classmethod
    def version(cls) -> str:
        """`{declared}+{digest}`: the declared `parser_version` and eight hex characters over the
        code that turns a file into tallies (this connector's module, this module and the shared
        classifiers it calls), so a change nobody bumped still restates every stored month, as the
        record connectors' effective parser version does (`Connector.effective_parser_version`)."""
        cached = _VERSIONS.get(cls)
        if cached is None:
            paths = {Path(inspect.getsourcefile(cls) or ""), Path(__file__)}
            paths.update(_REPO_ROOT / p for p in _SHARED_PARSER_MODULES)
            digest = hashlib.sha256()
            for path in sorted(paths):
                digest.update(_code_digest(path).encode("ascii"))
            cached = f"{cls.parser_version}+{digest.hexdigest()[:8]}"
            _VERSIONS[cls] = cached
        return cached


#: The shared modules whose code decides what a history connector's documents hold: the tallies
#: (the classifiers, the document helpers) and the data-quality verdict that decides whether a
#: month is served (`dq.py`), so a changed check re-judges every stored month.
_SHARED_PARSER_MODULES = (
    "pipeline/normalize.py",
    "pipeline/vendor/gridstatus/queues.py",
    "pipeline/connectors/history/documents.py",
    "pipeline/connectors/history/dq.py",
)
_REPO_ROOT = Path(__file__).resolve().parents[3]
_VERSIONS: dict[type, str] = {}


def parse_time(value: str) -> dt.datetime:
    when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return when if when.tzinfo is not None else when.replace(tzinfo=dt.UTC)


@dataclass(frozen=True)
class StoredSnapshot:
    """A snapshot another connector stored (`shares_fetch_with`): the bytes are the publisher's,
    fetched from its own server, so a history connector reads them instead of asking again."""

    ts: str
    sha256: str
    key: str
    fetched_url: str
    retrieved_at: str
    meta: Mapping[str, Any]


def stored_snapshots(store: Store, source_id: str) -> list[StoredSnapshot]:
    """One entry per distinct stored snapshot of `source_id`, oldest first: the run that wrote the
    object (an `unchanged` run points at an earlier run's object and is skipped), whose object still
    exists. Reads run records only; no bytes."""
    out: dict[str, StoredSnapshot] = {}
    for ts, record in store._run_entries(source_id):
        snap = record.get("snapshot") or {}
        sha = snap.get("sha256")
        located = str(snap.get("object_key") or "")
        if not sha or not located or sha in out:
            continue
        ext = located.rsplit("/", 1)[-1].rpartition(".")[2]
        if not ext:
            continue
        path = store.snapshot_path(source_id, ts, ext)
        if not store.exists(path):
            continue
        meta = snap.get("meta")
        out[str(sha)] = StoredSnapshot(
            ts=ts,
            sha256=str(sha),
            key=store.key(path),
            fetched_url=str(snap.get("fetched_url") or ""),
            retrieved_at=str(snap.get("retrieved_at") or record.get("started_at") or ""),
            meta=meta if isinstance(meta, Mapping) else {},
        )
    return list(out.values())


# ------------------------------------------------------------------------------------- tallies
def _thousandths(value: float) -> int:
    return round(value * 1000)


@dataclass
class Tally:
    """A count of rows and their MW (and, where the source states it, a second capacity measure).
    A sum over no stated value is `null`, never `0`; MW is kept in thousandths so sums are exact."""

    count: int = 0
    mw: int = 0
    mw_stated: int = 0
    alt: int = 0
    alt_stated: int = 0

    def add(self, mw: float | None, alt: float | None = None) -> None:
        self.count += 1
        if mw is not None:
            self.mw += _thousandths(mw)
            self.mw_stated += 1
        if alt is not None:
            self.alt += _thousandths(alt)
            self.alt_stated += 1

    def as_dict(self, alt_name: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "count": self.count,
            "mw": self.mw / 1000 if self.mw_stated else None,
            "mw_stated": self.mw_stated,
        }
        if alt_name:
            out[alt_name] = self.alt / 1000 if self.alt_stated else None
            out[f"{alt_name}_stated"] = self.alt_stated
        return out


@dataclass
class Block:
    """One counted population (a sheet, or the rows of one status family): a total and a `Tally`
    per key of each dimension. `basis` names what was counted, for the like-for-like rule
    (`documents.py`). `alt_name` names the second capacity measure where there is one."""

    basis: str
    dimensions: tuple[str, ...]
    alt_name: str | None = None
    total: Tally = field(default_factory=Tally)
    buckets: dict[str, dict[str, Tally]] = field(default_factory=dict)
    excluded: dict[str, Tally] = field(default_factory=dict)

    def add(self, keys: Mapping[str, str | None], mw: float | None, alt: float | None = None) -> None:
        self.total.add(mw, alt)
        for dimension in self.dimensions:
            key = keys.get(dimension)
            if key is None:
                key = UNSTATED
            self.buckets.setdefault(dimension, {}).setdefault(key, Tally()).add(mw, alt)

    def exclude(self, reason: str, mw: float | None) -> None:
        self.excluded.setdefault(reason, Tally()).add(mw)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "basis": self.basis,
            "total": self.total.as_dict(self.alt_name),
            "excluded": {k: v.as_dict() for k, v in sorted(self.excluded.items())},
        }
        for dimension in self.dimensions:
            tallies = self.buckets.get(dimension, {})
            out[f"by_{dimension}"] = {k: tallies[k].as_dict(self.alt_name) for k in sorted(tallies)}
        return out


#: The bucket of a row whose dimension the source leaves blank.
UNSTATED = "_unstated"
#: The `technology_labels` key of rows whose technology the source leaves blank: recorded, so a
#: document says it counted such rows in `unknown`, and judged by `dq.py`'s `technology_blank`.
BLANK_LABEL = "(blank)"


# ------------------------------------------------------------------------------------ workbooks
def normalise_header(value: Any) -> str:
    """A header cell as one line: EIA writes `\\n \\nNet Summer Capacity (MW)` in 2015."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def number(value: Any) -> float | None:
    """A numeric cell, or None for a blank (EIA writes a single space), text or a non-finite value."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        out = float(value)
    else:
        text = str(value).strip().replace(",", "")
        if not text:
            return None
        try:
            out = float(text)
        except ValueError:
            return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def year_of(value: Any) -> int | None:
    """A year from a numeric year cell or a date; None otherwise (and for EIA's blanks)."""
    if isinstance(value, (dt.datetime, dt.date)):
        return value.year
    n = number(value)
    if n is None or not 1900 <= n <= 2200 or n != int(n):
        return None
    return int(n)


def open_workbook(content: bytes) -> Any:
    """A read-only, values-only openpyxl workbook over `content`: rows stream from the sheet's XML,
    so a 14 MB workbook is never held as a cell tree."""
    import openpyxl

    if not content.startswith(XLSX_MAGIC):
        raise ParseError("not an xlsx workbook (no zip magic)")
    try:
        return openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise ParseError(f"workbook unreadable: {exc!r}") from exc


def find_header(
    rows: Iterable[Sequence[Any]], required: Sequence[str], *, within: int = 60
) -> tuple[int, list[str], list[Sequence[Any]]]:
    """The first row (0-based) among the first `within` whose cells include every `required` name,
    its normalised header, and the rows read before it (title and notes)."""
    seen: list[Sequence[Any]] = []
    for index, row in enumerate(rows):
        if index >= within:
            break
        header = [normalise_header(c) for c in row]
        if all(name in header for name in required):
            return index, header, seen
        seen.append(row)
    raise ParseError(f"no header row with {list(required)} in the first {within} rows")


def column(header: Sequence[str], *names: str) -> int | None:
    """The index of the first of `names` present in `header`."""
    for name in names:
        if name in header:
            return header.index(name)
    return None


def cell(row: Sequence[Any], index: int | None) -> Any:
    if index is None or index >= len(row):
        return None
    return row[index]


def text(value: Any) -> str | None:
    """A text cell stripped, None for a blank."""
    if value is None:
        return None
    out = str(value).strip()
    return out or None


__all__ = [
    "BLANK_LABEL",
    "MODES",
    "ROUTE_RANK",
    "UNSTATED",
    "XLSX_MAGIC",
    "ArchiveItem",
    "Block",
    "HeldPeriod",
    "HistoryConnector",
    "HistoryGateRefused",
    "MonthFile",
    "Tally",
    "cell",
    "column",
    "find_header",
    "gate",
    "normalise_header",
    "number",
    "open_workbook",
    "parse_time",
    "text",
    "year_of",
]
