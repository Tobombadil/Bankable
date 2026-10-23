"""Where the daily documents live, how they are written, mirrored and read back (`docs/24-trends.md`).

    <data root>/normalized/trends/daily/YYYY-MM-DD.json    one document per UTC day (`as_of`)
    <data root>/normalized/trends/uploaded.json            what the object store already holds
    s3://$R2_BUCKET/trends/daily/YYYY-MM-DD.json           the mirror, when R2 is configured

The data root is the connector data root (`INFRAQUE_DATA_DIR`, else this checkout's `data/`; in the
containers, the `connector_data` volume the worker mounts). A document is written to a temporary file
in the same directory, flushed to disk, renamed over the day's name, and the directory flushed too
(so the rename itself survives a power cut), so a reader sees the old document or the new one, never
part of either, and writing a day twice replaces it. A temporary file a killed write left behind is
removed by a later run once it is a day old (`TrendStore.sweep_temporaries`; younger, it may be a
write in flight).

**The mirror.** When the private object store is configured (`R2_BUCKET`, `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY`, and `R2_ENDPOINT` or `R2_ACCOUNT_ID`: the names the flood and parcels pulls
read, `pipeline/context/fema_nfhl.py::store_from_env`), every local document whose bytes the bucket
does not yet hold is uploaded under `trends/daily/`. The bucket is the private one, never the public
tiles bucket: a bucket named as `R2_TILES_BUCKET`, or whose name contains `tiles` in any case (the
public bucket is `<prefix>-tiles-production`, `infra/ci/beta.sh` `BUCKET_PREFIX`; so the guard holds
even where the worker's environment does not carry `R2_TILES_BUCKET`, which only the deploy's basemap
job is given), is refused. `uploaded.json` records the sha256 of
each uploaded document, so an upload that failed (store unreachable, credentials refused) is retried
by the next run, a replaced document is uploaded again, and a newly configured bucket receives the
whole series. `sync_uploads` never raises: the local file is the record, the bucket its copy.

**Reading.** `TrendStore.days` lists the days held, `load` reads one, `series` reads a range in day
order, skipping (and logging) a file that is not a readable document. Every schema version ever
written is readable (`snapshot.READABLE_SCHEMA_VERSIONS`); a document from a newer version than this
code knows is refused rather than misread.
"""

from __future__ import annotations

import datetime as dt
import errno
import hashlib
import json
import logging
import os
import re
import stat
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from services.trends.snapshot import DOCUMENT_KIND, READABLE_SCHEMA_VERSIONS

logger = logging.getLogger("services.trends.store")

#: Under the data root.
TRENDS_DIR = Path("normalized") / "trends"
DAILY_DIR = TRENDS_DIR / "daily"
UPLOAD_STATE = TRENDS_DIR / "uploaded.json"
#: Under the bucket's root (no `data/` prefix: the key names its own tree, as the flood pull's do).
OBJECT_PREFIX = "trends/daily/"
DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")
#: `_write_atomic`'s temporary file: `.<final name>.<32 hex>.tmp`.
TEMPORARY_FILE = re.compile(r"^\..+\.[0-9a-f]{32}\.tmp$")
#: A temporary file older than this is a killed write's leftover, never one in flight (the job's
#: timeout is 15 minutes).
STALE_TEMPORARY_SECONDS = 24 * 3600
#: Uploads per run at most: a newly configured bucket takes about a year's documents in one run.
MAX_UPLOADS_PER_RUN = 400
#: Bucket names the mirror never writes to, whatever `R2_TILES_BUCKET` says: the public tiles bucket.
PUBLIC_BUCKET_MARK = "tiles"

ENV_BUCKET = "R2_BUCKET"
ENV_TILES_BUCKET = "R2_TILES_BUCKET"
ENV_ENDPOINT = "R2_ENDPOINT"
ENV_ACCOUNT_ID = "R2_ACCOUNT_ID"
ENV_ACCESS_KEY_ID = "R2_ACCESS_KEY_ID"
ENV_SECRET_ACCESS_KEY = "R2_SECRET_ACCESS_KEY"  # noqa: S105 -- an environment variable name, not a value


class DocumentError(ValueError):
    """A document (or a file read as one) is not a daily trends document."""


def document_bytes(document: Mapping[str, Any]) -> bytes:
    """The document as stored: keys sorted and one per line, so two days diff line by line."""
    return (json.dumps(document, sort_keys=True, indent=1, ensure_ascii=True) + "\n").encode("ascii")


def _check(document: Mapping[str, Any]) -> dt.date:
    """The document's day, after checking it is one of ours, in a schema version this code reads."""
    version = document.get("schema_version")
    if document.get("kind") != DOCUMENT_KIND or type(version) is not int:
        raise DocumentError(f"not a {DOCUMENT_KIND} document")
    if version not in READABLE_SCHEMA_VERSIONS:
        readable = ", ".join(map(str, READABLE_SCHEMA_VERSIONS))
        raise DocumentError(f"schema_version {version} is not one this code reads ({readable})")
    try:
        return dt.date.fromisoformat(str(document.get("as_of")))
    except ValueError as exc:
        raise DocumentError(f"as_of {document.get('as_of')!r} is not a date") from exc


#: `fsync` on a directory is refused by some filesystems; the rename then is as durable as it gets.
_DIR_FSYNC_UNSUPPORTED = frozenset({errno.EINVAL, errno.ENOTSUP, errno.EBADF, errno.EACCES})


def _fsync_directory(directory: Path) -> None:
    """Flush `directory`'s entries, so a rename into it survives a power cut (`rename(2)` is atomic
    but not durable until the directory is flushed)."""
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


def _write_atomic(path: Path, data: bytes) -> None:
    """`data` at `path` by a same-directory temporary file, fsync, rename and a flush of the
    directory (module docstring)."""
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


class TrendStore:
    """The daily documents under one data root."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        self.daily = self.data_root / DAILY_DIR
        self.state_path = self.data_root / UPLOAD_STATE

    def path_for(self, day: dt.date) -> Path:
        return self.daily / f"{day.isoformat()}.json"

    def exists(self, day: dt.date) -> bool:
        return self.path_for(day).is_file()

    def write(self, document: Mapping[str, Any]) -> Path:
        """Write (or replace) the document for its `as_of` day; returns its path. Removes what an
        earlier killed write left (`sweep_temporaries`)."""
        path = self.path_for(_check(document))
        _write_atomic(path, document_bytes(document))
        self.sweep_temporaries()
        return path

    def sweep_temporaries(self, *, now: float | None = None) -> int:
        """Remove the temporary files `_write_atomic` left when a write was killed between creating
        and renaming one, once older than `STALE_TEMPORARY_SECONDS` (a younger one may be a write
        in flight). Every run of the job reaches it: `write` and `sync_uploads` both call it.
        Returns how many were removed; never raises."""
        cutoff = (time.time() if now is None else now) - STALE_TEMPORARY_SECONDS
        removed = 0
        for directory in (self.daily, self.data_root / TRENDS_DIR):
            try:
                entries = list(directory.iterdir())
            except OSError:  # not created yet, or unreadable: nothing to sweep here
                continue
            for entry in entries:
                if not TEMPORARY_FILE.match(entry.name):
                    continue
                try:
                    info = entry.lstat()
                    if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                        entry.unlink()
                        removed += 1
                except OSError as exc:
                    logger.warning("trends: could not remove %s: %s", entry.name, type(exc).__name__)
        if removed:
            logger.info("trends: removed %d temporary file(s) a killed write left", removed)
        return removed

    def read_bytes(self, day: dt.date) -> bytes:
        return self.path_for(day).read_bytes()

    def load(self, day: dt.date) -> dict[str, Any]:
        """The document for `day`; `FileNotFoundError` when none was recorded, `DocumentError` when
        the file is not a document for that day."""
        try:
            document = json.loads(self.read_bytes(day))
        except json.JSONDecodeError as exc:
            raise DocumentError(f"{self.path_for(day)} is not JSON") from exc
        if not isinstance(document, dict) or _check(document) != day:
            raise DocumentError(f"{self.path_for(day)} is not the document for {day}")
        return document

    def days(self) -> list[dt.date]:
        """Every day a document is held for, oldest first."""
        if not self.daily.is_dir():
            return []
        out = []
        for entry in self.daily.iterdir():
            match = DAY_FILE.match(entry.name)
            if match and entry.is_file():
                try:
                    out.append(dt.date.fromisoformat(match.group(1)))
                except ValueError:
                    continue
        return sorted(out)

    def series(self, start: dt.date | None = None, end: dt.date | None = None) -> Iterator[dict[str, Any]]:
        """The documents from `start` to `end` inclusive, oldest first; a day with no document is
        absent (a day not recorded is not invented), and an unreadable file is logged and skipped."""
        for day in self.days():
            if (start is not None and day < start) or (end is not None and day > end):
                continue
            try:
                yield self.load(day)
            except (OSError, DocumentError) as exc:
                logger.warning("trends: skipped %s: %s", self.path_for(day).name, exc)

    # -- what the bucket holds
    def upload_state(self, bucket: str) -> dict[str, str]:
        """`{day: sha256}` of the documents already uploaded to `bucket`; empty for another bucket or
        an unreadable state file (everything is then uploaded again, which is idempotent)."""
        try:
            state = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(state, dict) or state.get("bucket") != bucket:
            return {}
        objects = state.get("objects")
        return {str(k): str(v) for k, v in objects.items()} if isinstance(objects, dict) else {}

    def record_upload_state(self, bucket: str, objects: Mapping[str, str]) -> None:
        body = {"bucket": bucket, "objects": dict(sorted(objects.items()))}
        _write_atomic(self.state_path, (json.dumps(body, indent=1, sort_keys=True) + "\n").encode())


# -------------------------------------------------------------------------------- the mirror
class ObjectWriter(Protocol):
    """What the mirror needs from an object backend (`pipeline.connectors.objectstore.S3Backend`)."""

    def put(self, key: str, data: bytes, *, immutable: bool = False) -> None: ...


@dataclass(frozen=True)
class Mirror:
    """The private bucket the documents are copied to, and the backend that writes to it."""

    bucket: str
    backend: ObjectWriter


@dataclass(frozen=True)
class MirrorConfig:
    """`mirror_from_env`'s answer: a mirror, or why there is none (`not_configured`, `incomplete`,
    `refused`), with the detail an operator needs (variable names only, never a value)."""

    mirror: Mirror | None
    status: str
    detail: str | None = None


def mirror_from_env(env: Mapping[str, str] | None = None) -> MirrorConfig:
    """The mirror the environment configures (module docstring). Unset `R2_BUCKET` is
    `not_configured` (a laptop, a test); a bucket with a credential missing is `incomplete`; a bucket
    that is, or is named like, the public tiles bucket is `refused`."""
    env = os.environ if env is None else env
    bucket = (env.get(ENV_BUCKET) or "").strip()
    if not bucket:
        return MirrorConfig(None, "not_configured")
    tiles = (env.get(ENV_TILES_BUCKET) or "").strip()
    if tiles and tiles == bucket:
        return MirrorConfig(
            None, "refused", f"{ENV_BUCKET} names the public tiles bucket ({ENV_TILES_BUCKET})"
        )
    if PUBLIC_BUCKET_MARK in bucket.lower():
        return MirrorConfig(
            None,
            "refused",
            f"{ENV_BUCKET} contains {PUBLIC_BUCKET_MARK!r}, the public tiles bucket's naming; "
            "the documents go to the private bucket only",
        )
    endpoint = (env.get(ENV_ENDPOINT) or "").strip()
    account = (env.get(ENV_ACCOUNT_ID) or "").strip()
    if not endpoint and account:
        endpoint = f"https://{account}.r2.cloudflarestorage.com"
    key_id = (env.get(ENV_ACCESS_KEY_ID) or "").strip()
    secret = (env.get(ENV_SECRET_ACCESS_KEY) or "").strip()
    missing = [
        name
        for name, value in (
            (ENV_ACCESS_KEY_ID, key_id),
            (ENV_SECRET_ACCESS_KEY, secret),
            (f"{ENV_ENDPOINT} or {ENV_ACCOUNT_ID}", endpoint),
        )
        if not value
    ]
    if missing:
        return MirrorConfig(None, "incomplete", f"unset: {', '.join(missing)}")
    from pipeline.connectors.objectstore import S3Backend

    backend = S3Backend(
        bucket, endpoint_url=endpoint, access_key_id=key_id, secret_access_key=secret, prefix=""
    )
    return MirrorConfig(Mirror(bucket, backend), "configured")


def object_key(day: dt.date) -> str:
    return f"{OBJECT_PREFIX}{day.isoformat()}.json"


def sync_uploads(store: TrendStore, config: MirrorConfig) -> dict[str, Any]:
    """Upload every local document the bucket does not hold in its current bytes, oldest first, at
    most `MAX_UPLOADS_PER_RUN`. Stops at the first failure (an unreachable store fails every
    request alike) and reports it by exception class only. Never raises. Every run of the job calls
    it, so it first removes stale temporary files (`TrendStore.sweep_temporaries`), which a run that
    writes nothing would otherwise never do."""
    store.sweep_temporaries()
    if config.mirror is None:
        return {"status": config.status, "detail": config.detail, "uploaded": 0}
    mirror = config.mirror
    try:
        held = store.upload_state(mirror.bucket)
        pending: list[tuple[dt.date, bytes, str]] = []
        for day in store.days():
            data = store.read_bytes(day)
            digest = hashlib.sha256(data).hexdigest()
            if held.get(day.isoformat()) != digest:
                pending.append((day, data, digest))
    except OSError as exc:
        logger.warning("trends: could not read the local documents for upload: %s", type(exc).__name__)
        return {"status": "failed", "detail": type(exc).__name__, "uploaded": 0}
    uploaded = 0
    for day, data, digest in pending[:MAX_UPLOADS_PER_RUN]:
        try:
            mirror.backend.put(object_key(day), data)
        except Exception as exc:  # any failure of the medium: keep the local file, retry next run
            logger.warning(
                "trends: upload of %s to the private bucket failed (%s); retried next run",
                day.isoformat(),
                type(exc).__name__,
            )
            return {
                "status": "failed",
                "detail": type(exc).__name__,
                "uploaded": uploaded,
                "pending": len(pending) - uploaded,
            }
        held[day.isoformat()] = digest
        uploaded += 1
        try:
            store.record_upload_state(mirror.bucket, held)
        except OSError as exc:  # the next run uploads this day again, which is harmless
            logger.warning("trends: could not record the upload state: %s", type(exc).__name__)
    return {"status": "ok", "uploaded": uploaded, "pending": len(pending) - uploaded}


__all__ = [
    "DAILY_DIR",
    "OBJECT_PREFIX",
    "DocumentError",
    "Mirror",
    "MirrorConfig",
    "TrendStore",
    "document_bytes",
    "mirror_from_env",
    "object_key",
    "sync_uploads",
]
