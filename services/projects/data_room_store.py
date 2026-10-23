"""Where data-room files live, and how they are sealed (docs/19 §3; "Data rooms, as built").

**The switch.** Data rooms answer only while `DATA_ROOMS=on` *and* `MY_PIPELINE=on` (the API reads
both). Off by default: the routes are the one `404` an unknown address gets, and the pages show no
tab. `data_rooms_enabled` reads it per call, as the workspace switch is read.

**Private object storage, never a disk the site serves.** Files go to the private bucket
(`R2_BUCKET`, the raw-snapshot bucket, never the public tiles bucket), under the `data-rooms/`
prefix, with a random key (`new_object_key`: 128 random bits, nothing derived from a name, a title
or an id). `DATA_ROOM_STORE` picks the medium:

- `s3` (the default): any S3-compatible bucket through the R2 settings the beta's env file already
  carries (`R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, and `R2_ENDPOINT` or
  `R2_ACCOUNT_ID`), with the client `pipeline.connectors.objectstore` builds;
- `local`: a directory (`DATA_ROOM_LOCAL_DIR`) for development and the tests only. It is refused in
  any environment that is not a development one (`services.environment`), so a deployment can never
  keep documents on a container's disk.

**Sealed before they leave the process.** R2 encrypts every object at rest (AES-256, GCM, keys held by
Cloudflare: https://developers.cloudflare.com/r2/reference/data-security/). On top of that each file
is sealed here with AES-256-GCM under its own random key; that key is itself sealed under the
deployment's data-room key (`DATA_ROOM_KEY`, 32 bytes, base64, a secret held only in the env file)
and stored on the document's row (`wrapped_key`). Both seals bind the object key as associated data,
so an object cannot be swapped under another row. What that does and does not protect:

- **File contents need `DATA_ROOM_KEY`**, which is in the env file only: never in the bucket, the
  database or its backups. Whoever holds the bucket, a database dump, or both, still cannot open a
  file without it.
- **A bucket credential is not only ciphertext.** The nightly database backup
  (`infra/scripts/backup.sh`) is a plain `pg_dump` in the same bucket, under `postgres/`. So anyone
  holding a credential for `R2_BUCKET` (the R2 keys GitHub Actions holds for the layer builds, a
  leaked token) gets the sealed objects *and* every file's name, type, size and uploader, the
  access logs and the wrapped keys. Only the contents stay sealed.
- Deleting a document's row destroys its key in the live database, so a leftover object is
  unreadable from it before the retention job removes it; a backup taken before the delete still
  holds the wrapped key for its 35 days, which with `DATA_ROOM_KEY` opens the object until the
  object itself is gone.

Without a valid `DATA_ROOM_KEY` the data room is unavailable (`503`), never unencrypted.

**Deletes follow the commit of the root transaction.** `delete_after_commit` removes objects only
once the outermost transaction that deleted their rows has committed (a savepoint's release decides
nothing, and a savepoint's rollback forgets the deletes made inside it); `discard_on_rollback`
removes an object just written unless the root transaction commits its row. What a crash leaves
behind, the retention job sweeps (`services.retention.data_rooms`).

Nothing here logs a key's owner, a file name or a byte of content; a failed delete is logged as a
count.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import datetime as dt
import functools
import hashlib
import logging
import os
import pathlib
import secrets
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Any, Final, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction

from services.environment import is_dev_environment

logger = logging.getLogger("services.projects.data_room_store")

ENV_SWITCH: Final = "DATA_ROOMS"
ENV_STORE: Final = "DATA_ROOM_STORE"
ENV_LOCAL_DIR: Final = "DATA_ROOM_LOCAL_DIR"
ENV_KEY: Final = "DATA_ROOM_KEY"
ENV_MAX_FILE_MB: Final = "DATA_ROOM_MAX_FILE_MB"
ENV_QUOTA_MB: Final = "DATA_ROOM_QUOTA_MB"
ENV_TRANSFERS: Final = "DATA_ROOM_TRANSFERS"
STORE_MODES: Final = ("s3", "local")
#: Every data-room object sits under this prefix of the private bucket, beside `data/` (the pipeline's
#: snapshots) and `postgres/` (the nightly backups).
OBJECT_PREFIX: Final = "data-rooms/"
_OBJECT_KEY_LENGTH: Final = len(OBJECT_PREFIX) + 32

MIB: Final = 1024 * 1024
#: One file at most (docs/19 §3 "upload validation"). A file is sealed whole in memory on a 1 GB API
#: container, so the ceiling stays well under it; the default suits scanned permits and models.
DEFAULT_MAX_FILE_MB: Final = 25
MAX_FILE_MB_CEILING: Final = 50
#: What one user's uploads may take in all, across every item (docs/19 §3 "a per-user quota").
DEFAULT_QUOTA_MB: Final = 1024
QUOTA_MB_CEILING: Final = 100 * 1024
#: Uploads and downloads sealed or opened at once per API process: each holds about three copies
#: of a file in memory. An upload takes its slot before its body is read.
DEFAULT_TRANSFERS: Final = 2
TRANSFER_WAIT_SECONDS: Final = 20.0
#: How often a request waiting for a slot looks again (`transfer_slot_async`, which holds no thread).
TRANSFER_POLL_SECONDS: Final = 0.05
#: Uploads one user may have in progress at once per API process, so one account cannot take every
#: transfer slot (and the memory each holds) with parallel uploads; the hourly limit does not bound
#: concurrency.
USER_UPLOADS_IN_FLIGHT: Final = 1


class DataRoomUnavailable(RuntimeError):
    """The data room is misconfigured or its store cannot be reached. The message is for operators
    and names settings, never a file."""


class DataRoomBusy(RuntimeError):
    """Every transfer slot stayed taken for `TRANSFER_WAIT_SECONDS`."""


class UploadInProgress(RuntimeError):
    """The user already has `USER_UPLOADS_IN_FLIGHT` uploads in progress in this process."""


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def data_rooms_enabled(env: Mapping[str, str] | None = None) -> bool:
    """`DATA_ROOMS=on` (any case), and nothing else, turns the data room on."""
    return (_env(env).get(ENV_SWITCH) or "").strip().lower() == "on"


# ------------------------------------------------------------------------------------- limits
@dataclass(frozen=True)
class Limits:
    max_file_bytes: int
    quota_bytes: int


def _megabytes(env: Mapping[str, str], name: str, default: int, ceiling: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise DataRoomUnavailable(f"{name} must be a whole number of megabytes") from None
    if not 1 <= value <= ceiling:
        raise DataRoomUnavailable(f"{name} must be between 1 and {ceiling}")
    return value


def limits_from_env(env: Mapping[str, str] | None = None) -> Limits:
    source = _env(env)
    return Limits(
        max_file_bytes=_megabytes(source, ENV_MAX_FILE_MB, DEFAULT_MAX_FILE_MB, MAX_FILE_MB_CEILING) * MIB,
        quota_bytes=_megabytes(source, ENV_QUOTA_MB, DEFAULT_QUOTA_MB, QUOTA_MB_CEILING) * MIB,
    )


#: Room in an upload's body for the form around the file: the boundaries, each part's headers (the
#: file's name among them, as long as a browser sends it) and the folder id.
UPLOAD_FORM_OVERHEAD: Final = 64 * 1024
#: The largest request body an upload may send under any setting: the ceiling and the form.
UPLOAD_BODY_CEILING: Final = MAX_FILE_MB_CEILING * MIB + UPLOAD_FORM_OVERHEAD


def upload_body_limit(limits: Limits) -> int:
    """The largest body an upload may send under these limits: one file at the cap, and the form.
    Checked before the body is read (`services/api/my_projects.py::upload_length_refusal`), so no
    request makes the API hold more than this for an upload."""
    return limits.max_file_bytes + UPLOAD_FORM_OVERHEAD


# --------------------------------------------------------------------------------- the seals
_OBJECT_MAGIC: Final = b"IFDR\x01"
_WRAP_VERSION: Final = b"\x01"
_NONCE_BYTES: Final = 12
_TAG_BYTES: Final = 16
_OBJECT_AAD: Final = b"inframation:data-room:object:1:"
_KEY_AAD: Final = b"inframation:data-room:key:1:"
_FINGERPRINT_LABEL: Final = b"inframation:data-room:fingerprint:1:"


def new_key() -> str:
    """A fresh `DATA_ROOM_KEY`: 32 random bytes, URL-safe base64."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _decode_key(raw: str) -> bytes:
    text = raw.strip()
    for decode in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            key = decode(text + "=" * (-len(text) % 4))
        except (binascii.Error, ValueError):
            continue
        if len(key) == 32:
            return key
    raise DataRoomUnavailable(f"{ENV_KEY} must be 32 bytes, base64-encoded")


class Sealer:
    """AES-256-GCM, twice (module docstring): the file under its own key, that key under the
    deployment's."""

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != 32:
            raise DataRoomUnavailable(f"{ENV_KEY} must be 32 bytes")
        self._master = AESGCM(master_key)
        self.fingerprint = hashlib.sha256(_FINGERPRINT_LABEL + master_key).hexdigest()[:16]

    def seal(self, object_key: str, plaintext: bytes) -> tuple[bytearray, bytes]:
        """(the object's bytes, the sealed file key for its row). The object is written in place into
        one buffer (header, ciphertext, tag), so sealing holds one copy of the file besides the
        plaintext, not two."""
        file_key = AESGCM.generate_key(bit_length=256)
        nonce = secrets.token_bytes(_NONCE_BYTES)
        header = len(_OBJECT_MAGIC) + _NONCE_BYTES
        sealed = bytearray(header + len(plaintext) + _TAG_BYTES)
        sealed[:header] = _OBJECT_MAGIC + nonce
        AESGCM(file_key).encrypt_into(
            nonce, plaintext, _OBJECT_AAD + object_key.encode(), memoryview(sealed)[header:]
        )
        key_nonce = secrets.token_bytes(_NONCE_BYTES)
        wrapped = self._master.encrypt(key_nonce, file_key, _KEY_AAD + object_key.encode())
        return sealed, _WRAP_VERSION + key_nonce + wrapped

    def open(self, object_key: str, sealed: bytes, wrapped_key: bytes, fingerprint: str) -> bytes:
        """The plaintext, or `DataRoomUnavailable` when the key, the object or the row does not fit."""
        if fingerprint != self.fingerprint:
            raise DataRoomUnavailable("the document was sealed under another DATA_ROOM_KEY")
        if not sealed.startswith(_OBJECT_MAGIC) or not wrapped_key.startswith(_WRAP_VERSION):
            raise DataRoomUnavailable("the stored object is not a sealed data-room file")
        key_nonce = wrapped_key[1 : 1 + _NONCE_BYTES]
        nonce = sealed[len(_OBJECT_MAGIC) : len(_OBJECT_MAGIC) + _NONCE_BYTES]
        try:
            file_key = self._master.decrypt(
                key_nonce, wrapped_key[1 + _NONCE_BYTES :], _KEY_AAD + object_key.encode()
            )
            # A view, not a slice: the ciphertext is not copied before it is opened.
            body = memoryview(sealed)[len(_OBJECT_MAGIC) + _NONCE_BYTES :]
            return AESGCM(file_key).decrypt(nonce, body, _OBJECT_AAD + object_key.encode())
        except InvalidTag:
            raise DataRoomUnavailable("a sealed data-room file failed its integrity check") from None


def sealer_from_env(env: Mapping[str, str] | None = None) -> Sealer:
    raw = (_env(env).get(ENV_KEY) or "").strip()
    if not raw:
        raise DataRoomUnavailable(f"{ENV_KEY} is not set")
    return Sealer(_decode_key(raw))


# -------------------------------------------------------------------------------- the stores
def new_object_key() -> str:
    """`data-rooms/` and 32 random hex digits: nothing about the file, the item or the user."""
    return f"{OBJECT_PREFIX}{secrets.token_hex(16)}"


def is_object_key(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _OBJECT_KEY_LENGTH
        and value.startswith(OBJECT_PREFIX)
        and all(c in "0123456789abcdef" for c in value[len(OBJECT_PREFIX) :])
    )


def _checked(key: str) -> str:
    if not is_object_key(key):
        raise ValueError("not a data-room object key")
    return key


class ObjectStore(Protocol):
    name: str

    def put(self, key: str, data: bytes | bytearray) -> None: ...

    def get(self, key: str) -> bytes:
        """The object's bytes; `FileNotFoundError` when it is absent."""
        ...

    def delete(self, key: str) -> None:
        """Removes the object; an absent one is not an error."""
        ...

    def list_objects(self) -> Iterator[tuple[str, dt.datetime]]:
        """Every data-room object key with its last-modified time (UTC)."""
        ...


class LocalObjectStore:
    """A directory, for development and the tests (refused elsewhere: `store_from_env`). Files are
    written owner-only, temp-then-rename."""

    name = "local"

    def __init__(self, root: pathlib.Path) -> None:
        self.root = pathlib.Path(root)

    def _path(self, key: str) -> pathlib.Path:
        return self.root / _checked(key)

    def put(self, key: str, data: bytes | bytearray) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temp, path)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def list_objects(self) -> Iterator[tuple[str, dt.datetime]]:
        folder = self.root / OBJECT_PREFIX.rstrip("/")
        if not folder.is_dir():
            return
        for path in sorted(folder.iterdir()):
            key = f"{OBJECT_PREFIX}{path.name}"
            if is_object_key(key):
                yield key, dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC)


def _client_error_status(exc: BaseException) -> tuple[str, int | None]:
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return "", None
    error = response.get("Error") or {}
    meta = response.get("ResponseMetadata") or {}
    status = meta.get("HTTPStatusCode")
    return str(error.get("Code") or ""), int(status) if status is not None else None


class S3ObjectStore:
    """An S3-compatible bucket (Cloudflare R2 in production). The client is injectable."""

    name = "s3"

    def __init__(self, bucket: str, client: Any) -> None:
        if not bucket:
            raise DataRoomUnavailable("R2_BUCKET is not set")
        self.bucket = bucket
        self.client = client

    def put(self, key: str, data: bytes | bytearray) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=_checked(key),
                Body=data,
                ContentType="application/octet-stream",
            )
        except Exception as exc:
            raise DataRoomUnavailable(f"the object store refused a write ({type(exc).__name__})") from None

    def get(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=_checked(key))
            body: bytes = response["Body"].read()
        except Exception as exc:
            code, status = _client_error_status(exc)
            if code in ("NoSuchKey", "404", "NotFound") or status == 404:
                raise FileNotFoundError(key) from None
            raise DataRoomUnavailable(f"the object store refused a read ({type(exc).__name__})") from None
        return body

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=_checked(key))
        except Exception as exc:
            code, status = _client_error_status(exc)
            if code in ("NoSuchKey", "404", "NotFound") or status == 404:
                return
            raise DataRoomUnavailable(f"the object store refused a delete ({type(exc).__name__})") from None

    def list_objects(self) -> Iterator[tuple[str, dt.datetime]]:
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=OBJECT_PREFIX):
                for obj in page.get("Contents") or []:
                    key = str(obj["Key"])
                    modified = obj.get("LastModified")
                    if not isinstance(modified, dt.datetime):
                        modified = dt.datetime.now(dt.UTC)
                    if modified.tzinfo is None:
                        modified = modified.replace(tzinfo=dt.UTC)
                    if is_object_key(key):
                        yield key, modified
        except DataRoomUnavailable:
            raise
        except Exception as exc:
            raise DataRoomUnavailable(f"the object store refused a listing ({type(exc).__name__})") from None


@functools.lru_cache(maxsize=4)
def _s3_client(endpoint: str, access_key_id: str, secret_access_key: str) -> Any:
    from pipeline.connectors.objectstore import build_s3_client

    return build_s3_client(endpoint, access_key_id, secret_access_key)


def store_from_env(env: Mapping[str, str] | None = None) -> ObjectStore:
    """The store `DATA_ROOM_STORE` names (module docstring). Raises `DataRoomUnavailable`, naming
    the settings that are missing, rather than fall back to anything else."""
    source = _env(env)
    mode = (source.get(ENV_STORE) or "s3").strip().lower() or "s3"
    if mode not in STORE_MODES:
        raise DataRoomUnavailable(f"{ENV_STORE} must be one of {', '.join(STORE_MODES)}")
    if mode == "local":
        if not is_dev_environment():
            raise DataRoomUnavailable(f"{ENV_STORE}=local is for development only; use s3")
        root = (source.get(ENV_LOCAL_DIR) or "").strip()
        if not root:
            raise DataRoomUnavailable(f"{ENV_STORE}=local needs {ENV_LOCAL_DIR}")
        return LocalObjectStore(pathlib.Path(root))
    endpoint = (source.get("R2_ENDPOINT") or "").strip()
    account = (source.get("R2_ACCOUNT_ID") or "").strip()
    if not endpoint and account:
        endpoint = f"https://{account}.r2.cloudflarestorage.com"
    values = {
        "R2_BUCKET": (source.get("R2_BUCKET") or "").strip(),
        "R2_ACCESS_KEY_ID": (source.get("R2_ACCESS_KEY_ID") or "").strip(),
        "R2_SECRET_ACCESS_KEY": (source.get("R2_SECRET_ACCESS_KEY") or "").strip(),
        "R2_ENDPOINT or R2_ACCOUNT_ID": endpoint,
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise DataRoomUnavailable(f"the data-room store needs {', '.join(missing)}")
    client = _s3_client(endpoint, values["R2_ACCESS_KEY_ID"], values["R2_SECRET_ACCESS_KEY"])
    return S3ObjectStore(values["R2_BUCKET"], client)


# ------------------------------------------------------------------------- after the commit
# Only the end of the root transaction decides what happens to an object (review of 2026-10-23):
# SQLAlchemy also fires `after_commit` when a savepoint is released, and acting then removed files
# whose rows a later rollback restored. Each registration remembers the transaction it was made in
# (the innermost savepoint, or the root):
#
# - a savepoint rolled back forgets the deletes registered inside it (their rows are back), and its
#   new objects become orphans (their rows are gone), removed when the root ends either way;
# - a savepoint released changes nothing: the root still decides;
# - the root committed removes the scheduled deletes and the orphans and keeps the new objects;
# - the root ended any other way (rolled back, or the session closed without a commit) removes the
#   new objects and the orphans and keeps everything scheduled for deletion.
_PENDING: Final = "data_room.pending"
_ROOT_COMMITTED: Final = "data_room.root_committed"
_HOOKED: Final = "data_room.hooked"
_DELETE: Final = "delete"
_WRITTEN: Final = "written"
_ORPHAN: Final = "orphan"


@dataclass
class _Pending:
    transaction: SessionTransaction
    kind: str
    store: ObjectStore
    key: str


def _remove(pairs: list[tuple[ObjectStore, str]], what: str) -> None:
    failed = 0
    for store, key in pairs:
        try:
            store.delete(key)
        except Exception:  # an object left behind is swept by the retention job
            failed += 1
    if failed:
        logger.warning(
            "data-room objects not removed; the retention job will sweep them",
            extra={"failed_count": failed, "outcome": what},
        )


def _within(transaction: SessionTransaction | None, ancestor: SessionTransaction) -> bool:
    while transaction is not None:
        if transaction is ancestor:
            return True
        transaction = transaction.parent
    return False


def _boundary(transaction: SessionTransaction) -> SessionTransaction:
    """The savepoint or root a rollback of `transaction` undoes (an inner, non-nested transaction,
    such as a flush's, rolls back to the nearest one)."""
    while transaction.parent is not None and not transaction.nested:
        transaction = transaction.parent
    return transaction


def _after_commit(session: Session) -> None:
    if session.in_nested_transaction():
        return  # a savepoint released: the root still decides
    session.info[_ROOT_COMMITTED] = True


def _after_soft_rollback(session: Session, previous_transaction: SessionTransaction) -> None:
    boundary = _boundary(previous_transaction)
    if not boundary.nested:
        return  # the root: `_after_transaction_end` decides
    kept: list[_Pending] = []
    for entry in session.info.get(_PENDING, []):
        if _within(entry.transaction, boundary):
            if entry.kind == _DELETE:
                continue  # the savepoint's rollback restored the rows: keep their objects
            if entry.kind == _WRITTEN:
                entry.kind = _ORPHAN  # its row is gone, whatever the root does
        kept.append(entry)
    session.info[_PENDING] = kept


def _after_transaction_end(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is not None:
        return
    committed = bool(session.info.pop(_ROOT_COMMITTED, False))
    pending: list[_Pending] = session.info.pop(_PENDING, None) or []
    removed = (_DELETE, _ORPHAN) if committed else (_WRITTEN, _ORPHAN)
    doomed = [(entry.store, entry.key) for entry in pending if entry.kind in removed]
    if doomed:
        _remove(doomed, "after_commit" if committed else "after_rollback")


def _hook(db: Session) -> SessionTransaction:
    """The listeners, once per session, and a transaction begun so its end fires them (a session
    that has not begun one commits or rolls back nothing). Returns the transaction a registration
    belongs to: the innermost savepoint, or the root."""
    if not db.info.get(_HOOKED):
        event.listen(db, "after_commit", _after_commit)
        event.listen(db, "after_soft_rollback", _after_soft_rollback)
        event.listen(db, "after_transaction_end", _after_transaction_end)
        db.info[_HOOKED] = True
    db.connection()
    transaction = db.get_nested_transaction() or db.get_transaction()
    if transaction is None:  # pragma: no cover - `connection()` has just begun one
        raise RuntimeError("no transaction to attach the data-room objects to")
    return transaction


def _register(db: Session, kind: str, store: ObjectStore, keys: list[str]) -> None:
    transaction = _hook(db)
    db.info.setdefault(_PENDING, []).extend(_Pending(transaction, kind, store, _checked(k)) for k in keys)


def delete_after_commit(db: Session, store: ObjectStore, keys: list[str]) -> None:
    """Remove these objects once `db`'s root transaction commits the deletion of their rows; forget
    them if it, or the savepoint they were scheduled in, rolls back."""
    if keys:
        _register(db, _DELETE, store, keys)


def discard_on_rollback(db: Session, store: ObjectStore, key: str) -> None:
    """Remove this just-written object unless `db`'s root transaction commits its row."""
    _register(db, _WRITTEN, store, [key])


# ---------------------------------------------------------------------------- transfer slots
_slots_lock = threading.Lock()
_slots: dict[int, threading.BoundedSemaphore] = {}


def _semaphore(count: int) -> threading.BoundedSemaphore:
    with _slots_lock:
        if count not in _slots:
            _slots[count] = threading.BoundedSemaphore(count)
        return _slots[count]


def transfer_count(env: Mapping[str, str] | None = None) -> int:
    raw = (_env(env).get(ENV_TRANSFERS) or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_TRANSFERS
    except ValueError:
        value = DEFAULT_TRANSFERS
    return max(1, min(value, 16))


@contextmanager
def transfer_slot(*, wait_seconds: float = TRANSFER_WAIT_SECONDS) -> Iterator[None]:
    """Bounds the files sealed or opened at once in this process (`DATA_ROOM_TRANSFERS`)."""
    semaphore = _semaphore(transfer_count())
    if not semaphore.acquire(timeout=wait_seconds):
        raise DataRoomBusy("every data-room transfer slot is taken")
    try:
        yield
    finally:
        semaphore.release()


@asynccontextmanager
async def transfer_slot_async(
    *, wait_seconds: float = TRANSFER_WAIT_SECONDS, poll_seconds: float = TRANSFER_POLL_SECONDS
) -> AsyncIterator[None]:
    """`transfer_slot` for the event loop: the same slots, waited for without holding a thread (a
    blocked `acquire` would hold one of the server's worker threads for up to `wait_seconds`)."""
    semaphore = _semaphore(transfer_count())
    deadline = time.monotonic() + wait_seconds
    while not semaphore.acquire(blocking=False):
        if time.monotonic() >= deadline:
            raise DataRoomBusy("every data-room transfer slot is taken")
        await asyncio.sleep(poll_seconds)
    try:
        yield
    finally:
        semaphore.release()


_uploads_lock = threading.Lock()
_uploads_in_flight: dict[str, int] = {}


@contextmanager
def user_upload(user_key: str, *, limit: int = USER_UPLOADS_IN_FLIGHT) -> Iterator[None]:
    """Counts one upload in progress for this user (an opaque key: the caller passes an id, never a
    name) and refuses one over `limit` at once (`UploadInProgress`), without waiting."""
    with _uploads_lock:
        if _uploads_in_flight.get(user_key, 0) >= limit:
            raise UploadInProgress("another upload by this user is in progress")
        _uploads_in_flight[user_key] = _uploads_in_flight.get(user_key, 0) + 1
    try:
        yield
    finally:
        with _uploads_lock:
            left = _uploads_in_flight.get(user_key, 1) - 1
            if left > 0:
                _uploads_in_flight[user_key] = left
            else:
                _uploads_in_flight.pop(user_key, None)


# ------------------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None, *, write: Callable[[str], Any] = sys.stdout.write) -> int:
    """`python -m services.projects.data_room_store new-key` prints a fresh `DATA_ROOM_KEY`."""
    args = sys.argv[1:] if argv is None else argv
    if args != ["new-key"]:
        write("usage: python -m services.projects.data_room_store new-key\n")
        return 2
    write(new_key() + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through `main()` in tests
    sys.exit(main())
