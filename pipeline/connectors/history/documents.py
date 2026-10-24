"""Monthly history documents: one JSON document per source and month (`docs/24-trends.md` §9).

    <data root>/normalized/trends/monthly/<source_id>/YYYY-MM.json

A document is one month of one official archive (an EIA-860M workbook, an ERCOT GIS report)
reduced to tallies: generators or projects counted and their MW summed, in total and by each
dimension the source states (technology, state, status, expected year, ...). It is written by the
history runner (`pipeline/connectors/history/runner.py`) and read by the trends series
(`services/trends/series.py`) and `GET /v1/trends`. The raw file it was computed from is stored
once as a snapshot (`raw.snapshot`), so every figure traces back to its bytes and can be
recomputed when the parser changes.

**Like for like.** Every block carries a `basis`: a short string naming what was counted and how
(the sheet, the capacity measure, the geography, any coverage rule the publisher changed). Two
months are compared only when their bases are equal; a series never crosses a change of basis
silently (the API leaves the change empty there and lists the break).

**Writing.** A document goes to a temporary file in the same directory, is flushed, renamed over
the month's name and the directory flushed, so a reader sees the old document or the new one,
never part of either. Writing a month twice replaces it; nothing else is ever deleted, so a run
that fails keeps the last good document of every month.

**Reading.** `MonthlyStore.periods`, `load` and `documents` read back every schema version ever
written (`READABLE_SCHEMA_VERSIONS`) and refuse a newer one rather than misread it. A `dq.status`
of `held` marks a document the data-quality checks held (`pipeline/connectors/history/dq.py`);
readers skip it until an operator releases it.

**A held reading never replaces a served month.** When the month already has a served document,
a newer reading its checks hold (a re-issue, a correction, a restatement, a file posted under the
wrong month's name) is written beside it as `YYYY-MM.held.json` (`write_pending`), and the served
document stays until an operator releases the held one (`release`), which then replaces it. A
month with no served document takes the held reading under its own name, as before, and readers
skip it.

This module imports nothing from `services`: `pipeline` may not import the API layer
(`infra/importlinter.ini`), and the trends package that reads these documents does.
"""

from __future__ import annotations

import datetime as dt
import errno
import json
import logging
import os
import re
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

log = logging.getLogger("pipeline.connectors.history.documents")

DOCUMENT_KIND = "inframation.trends.monthly"
#: Bumped when a key's meaning changes; a new key alone does not bump it. 1: 2026-10-23.
SCHEMA_VERSION = 1
READABLE_SCHEMA_VERSIONS: tuple[int, ...] = (1,)
#: Under the data root.
MONTHLY_DIR = Path("normalized") / "trends" / "monthly"
PERIOD_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
DQ_HELD = "held"
#: The suffix of a held reading kept beside a served month (module docstring).
PENDING_SUFFIX = ".held.json"

MONTH_NAMES: tuple[str, ...] = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


class DocumentError(ValueError):
    """A file read as a monthly document is not one, or not one this code reads."""


def period_of(year: int, month: int) -> str:
    if not 1 <= month <= 12:
        raise ValueError(f"month {month} out of range")
    return f"{year:04d}-{month:02d}"


def month_number(name: str) -> int | None:
    """`1` for `January`, `Jan` or `jan.` (`Sept` too); None for anything that is not a month
    name or a prefix of one at least three letters long."""
    token = name.strip().lower().rstrip(".")
    if len(token) < 3:
        return None
    for index, full in enumerate(MONTH_NAMES, start=1):
        if full.startswith(token):
            return index
    return None


def shift_period(period: str, months: int) -> str:
    """`2026-01`, -1 -> `2025-12`."""
    match = PERIOD_RE.match(period)
    if match is None:
        raise ValueError(f"{period!r} is not a YYYY-MM period")
    index = int(match.group(1)) * 12 + int(match.group(2)) - 1 + months
    return period_of(index // 12, index % 12 + 1)


def document_bytes(document: Mapping[str, Any]) -> bytes:
    """The document as stored: keys sorted, one per line, so two months diff line by line."""
    return (json.dumps(document, sort_keys=True, indent=1, ensure_ascii=True) + "\n").encode("ascii")


def check(document: Mapping[str, Any]) -> tuple[str, str]:
    """`(source_id, period)` of a document, after checking it is one of ours in a version this
    code reads."""
    version = document.get("schema_version")
    if document.get("kind") != DOCUMENT_KIND or type(version) is not int:
        raise DocumentError(f"not a {DOCUMENT_KIND} document")
    if version not in READABLE_SCHEMA_VERSIONS:
        readable = ", ".join(map(str, READABLE_SCHEMA_VERSIONS))
        raise DocumentError(f"schema_version {version} is not one this code reads ({readable})")
    source_id = document.get("source_id")
    period = document.get("period")
    if not isinstance(source_id, str) or not source_id:
        raise DocumentError("document has no source_id")
    if not isinstance(period, str) or PERIOD_RE.match(period) is None:
        raise DocumentError(f"period {period!r} is not YYYY-MM")
    if not isinstance(document.get("blocks"), Mapping):
        raise DocumentError("document has no blocks")
    return source_id, period


def is_held(document: Mapping[str, Any]) -> bool:
    dq = document.get("dq")
    return isinstance(dq, Mapping) and dq.get("status") == DQ_HELD and not dq.get("released_at")


_DIR_FSYNC_UNSUPPORTED = frozenset({errno.EINVAL, errno.ENOTSUP, errno.EBADF, errno.EACCES})


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        if exc.errno in _DIR_FSYNC_UNSUPPORTED:
            return
        raise
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in _DIR_FSYNC_UNSUPPORTED:
            raise
    finally:
        os.close(fd)


def write_atomic(path: Path, data: bytes) -> None:
    """`data` at `path` by a same-directory temporary file, fsync, rename and a directory flush."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


class MonthlyStore:
    """The monthly documents under one data root."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        self.root = self.data_root / MONTHLY_DIR

    def dir_for(self, source_id: str) -> Path:
        if "/" in source_id or source_id.startswith("."):
            raise ValueError(f"{source_id!r} is not a source id")
        return self.root / source_id

    def path_for(self, source_id: str, period: str) -> Path:
        if PERIOD_RE.match(period) is None:
            raise ValueError(f"{period!r} is not a YYYY-MM period")
        return self.dir_for(source_id) / f"{period}.json"

    def write(self, document: Mapping[str, Any]) -> Path:
        source_id, period = check(document)
        path = self.path_for(source_id, period)
        write_atomic(path, document_bytes(document))
        return path

    # ------------------------------------------------------------- held readings beside a month
    def pending_path_for(self, source_id: str, period: str) -> Path:
        return self.path_for(source_id, period).with_name(f"{period}{PENDING_SUFFIX}")

    def write_pending(self, document: Mapping[str, Any]) -> Path:
        """A held reading beside the month's served document (module docstring)."""
        source_id, period = check(document)
        if not is_held(document):
            raise DocumentError(f"{source_id} {period}: only a held reading is kept beside a month")
        path = self.pending_path_for(source_id, period)
        write_atomic(path, document_bytes(document))
        return path

    def load_pending(self, source_id: str, period: str) -> dict[str, Any] | None:
        """The held reading beside a month, or None when there is none."""
        path = self.pending_path_for(source_id, period)
        try:
            document = json.loads(path.read_bytes())
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise DocumentError(f"{path} is not JSON") from exc
        if not isinstance(document, dict) or check(document) != (source_id, period):
            raise DocumentError(f"{path} is not a held reading of {source_id} {period}")
        return document

    def pending(self, source_id: str) -> Iterator[dict[str, Any]]:
        """Every held reading kept beside a month, oldest month first; an unreadable one is logged
        and skipped."""
        directory = self.dir_for(source_id)
        if not directory.is_dir():
            return
        for entry in sorted(directory.iterdir()):
            name = entry.name
            if not name.endswith(PENDING_SUFFIX) or not entry.is_file():
                continue
            period = name[: -len(PENDING_SUFFIX)]
            if PERIOD_RE.match(period) is None:
                continue
            try:
                document = self.load_pending(source_id, period)
            except (OSError, DocumentError) as exc:
                log.warning("history: skipped held reading %s %s: %s", source_id, period, exc)
                continue
            if document is not None:
                yield document

    def discard_pending(self, source_id: str, period: str) -> bool:
        """Remove the held reading beside a month (a later reading superseded it)."""
        path = self.pending_path_for(source_id, period)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        _fsync_directory(path.parent)
        return True

    def periods(self, source_id: str) -> list[str]:
        """Every month a document is held for, oldest first."""
        directory = self.dir_for(source_id)
        if not directory.is_dir():
            return []
        out = []
        for entry in directory.iterdir():
            name = entry.name
            if name.endswith(".json") and PERIOD_RE.match(name[:-5]) and entry.is_file():
                out.append(name[:-5])
        return sorted(out)

    def stat_key(self, source_id: str, period: str) -> tuple[int, int] | None:
        """`(mtime_ns, size)` of a month's file, the cache key a reader uses; None when absent."""
        try:
            info = self.path_for(source_id, period).stat()
        except OSError:
            return None
        return info.st_mtime_ns, info.st_size

    def load(self, source_id: str, period: str) -> dict[str, Any]:
        path = self.path_for(source_id, period)
        try:
            document = json.loads(path.read_bytes())
        except json.JSONDecodeError as exc:
            raise DocumentError(f"{path} is not JSON") from exc
        if not isinstance(document, dict) or check(document) != (source_id, period):
            raise DocumentError(f"{path} is not the document for {source_id} {period}")
        return document

    def documents(
        self, source_id: str, start: str | None = None, end: str | None = None
    ) -> Iterator[dict[str, Any]]:
        """The documents from `start` to `end` inclusive, oldest first; an unreadable file is
        logged and skipped (a month not recorded is not invented)."""
        for period in self.periods(source_id):
            if (start is not None and period < start) or (end is not None and period > end):
                continue
            try:
                yield self.load(source_id, period)
            except (OSError, DocumentError) as exc:
                log.warning("history: skipped %s %s: %s", source_id, period, exc)

    def release(self, source_id: str, period: str, *, released_by: str, now: dt.datetime) -> Path:
        """Mark a held month released (an operator read it and accepts it); readers then serve it.
        A held reading kept beside a served month is the one released: it replaces the month's
        document, and the file beside it is removed."""
        pending = self.load_pending(source_id, period)
        document = pending if pending is not None else self.load(source_id, period)
        dq = dict(document.get("dq") or {})
        if dq.get("status") != DQ_HELD or dq.get("released_at"):
            raise DocumentError(f"{source_id} {period} is not held")
        dq["released_at"] = now.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        dq["released_by"] = released_by
        document["dq"] = dq
        path = self.write(document)
        if pending is not None:
            self.discard_pending(source_id, period)
        return path


__all__ = [
    "DOCUMENT_KIND",
    "DQ_HELD",
    "MONTHLY_DIR",
    "PENDING_SUFFIX",
    "PERIOD_RE",
    "READABLE_SCHEMA_VERSIONS",
    "SCHEMA_VERSION",
    "DocumentError",
    "MonthlyStore",
    "check",
    "document_bytes",
    "is_held",
    "month_number",
    "period_of",
    "shift_period",
    "write_atomic",
]
