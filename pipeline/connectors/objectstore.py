"""Object backends behind the connector store (docs/20 §2, §3.2, §12; docs/60 §5).

`pipeline.connectors.store.Store` decides *what* is written (snapshots, normalised and event
parquet, held rows, run records) and *where* in the key space; a backend here decides how bytes
reach a medium. Two exist:

* `LocalBackend` — files under a data root. Today's behaviour and the default: tests, the CLI on a
  laptop, and a single-host deployment with a writable data directory.
* `S3Backend` — any S3-compatible bucket (Cloudflare R2 in production, MinIO in the local proof).
  Selected with `SNAPSHOT_STORE=s3`. Fetch and load run on different worker hosts that share no
  filesystem; this is what carries a fetch's output to its load (docs/20 §2: stages talk only
  through the database and object storage).

Keys are the store's relative paths (`snapshots/{source_id}/{ts}.{ext}`, `runs/...`,
`quarantine/...`), so a local tree and a bucket prefix are interchangeable: `aws s3 sync data/
s3://$R2_BUCKET/data/` moves one into the other.

Failure contract (docs/20 §12 "object storage unavailable -> runs fail closed"): a failed write
raises `StoreWriteError` and leaves no partial object under the key (local writes go to a temporary
file and are renamed into place; an S3 PUT is atomic). An immutable write whose key already holds
different bytes raises `ImmutableObjectExists`; identical bytes are accepted as a no-op, so a replay
is idempotent. Reads raise `FileNotFoundError` for a missing key and `StoreReadError` otherwise.

The S3 client library (boto3) is imported lazily inside `S3Backend`, so the local backend and the
test suite need neither the package's credentials nor network access.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import pathlib
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

#: Environment variables (docs/60 §5). The R2 names are the ones `infra/scripts/backup.sh` and
#: `restore_drill.sh` already read; only the first, third and last are new.
ENV_MODE = "SNAPSHOT_STORE"
ENV_BUCKET = "R2_BUCKET"
ENV_ENDPOINT = "R2_ENDPOINT"
ENV_ACCOUNT_ID = "R2_ACCOUNT_ID"
ENV_ACCESS_KEY_ID = "R2_ACCESS_KEY_ID"
ENV_SECRET_ACCESS_KEY = "R2_SECRET_ACCESS_KEY"  # noqa: S105 -- an environment variable name, not a value
ENV_PREFIX = "SNAPSHOT_STORE_PREFIX"

MODES = ("local", "s3")
#: Default key prefix inside the bucket. The bucket also holds `postgres/` (backup.sh), so the
#: pipeline's tree sits under `data/`, mirroring the repo's own `data/` directory.
DEFAULT_PREFIX = "data/"


class StoreError(RuntimeError):
    """Base class for object-store failures."""


class StoreConfigError(StoreError):
    """`SNAPSHOT_STORE` names an unknown mode, or `s3` is selected without its settings. Never
    falls back to the local backend: a worker that silently wrote to its own disk would strand
    the run's output where the load job on another host cannot see it."""


class StoreWriteError(StoreError):
    """An object write failed (medium unavailable, permission, network). The run fails closed."""


class StoreReadError(StoreError):
    """An object read failed for a reason other than the key being absent."""


class ImmutableObjectExists(StoreError):
    """An immutable key (a raw snapshot) already holds different bytes; refusing to overwrite."""


class ObjectBackend(Protocol):
    """What `Store` needs from a medium. Keys are relative POSIX paths (`runs/a.b/t.json`)."""

    name: str

    def put(self, key: str, data: bytes, *, immutable: bool = False) -> None: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def list(self, prefix: str) -> list[str]:
        """Keys directly under `prefix` (a `dir/` string; no recursion), sorted ascending."""
        ...

    def delete(self, key: str) -> None: ...

    def uri(self, key: str) -> str: ...


def _check_key(key: str) -> str:
    parts = key.split("/")
    if not key or key.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"invalid object key {key!r}")
    return key


# ------------------------------------------------------------------------------------ local
class LocalBackend:
    """Files under `root`. Writes are temp-file-then-rename, so a crash or a full disk never leaves a
    truncated object under its final name."""

    name = "local"

    def __init__(self, root: pathlib.Path) -> None:
        self.root = pathlib.Path(root)

    def _path(self, key: str) -> pathlib.Path:
        return self.root / _check_key(key)

    def put(self, key: str, data: bytes, *, immutable: bool = False) -> None:
        path = self._path(key)
        if immutable and path.exists():
            if path.read_bytes() == data:
                return
            raise ImmutableObjectExists(f"{path} already exists with different content")
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise StoreWriteError(f"write to {path} failed: {exc}") from exc

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list(self, prefix: str) -> list[str]:
        directory = self.root / prefix if prefix else self.root
        if not directory.is_dir():
            return []
        return sorted(
            f"{prefix}{p.name}" for p in directory.iterdir() if p.is_file() and not p.name.startswith(".")
        )

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def uri(self, key: str) -> str:
        return str(self._path(key))


# --------------------------------------------------------------------------------------- s3
def _error_code(exc: BaseException) -> tuple[str, int | None]:
    """(`Error.Code`, HTTP status) of a botocore `ClientError`, read structurally so this module
    never imports botocore."""
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return "", None
    error = response.get("Error") or {}
    meta = response.get("ResponseMetadata") or {}
    status = meta.get("HTTPStatusCode")
    return str(error.get("Code") or ""), int(status) if status is not None else None


_MISSING = {"NoSuchKey", "404", "NotFound"}
_CONFLICT = {"PreconditionFailed", "ConditionalRequestConflict"}


def build_s3_client(endpoint_url: str, access_key_id: str, secret_access_key: str) -> Any:
    """A boto3 S3 client for an S3-compatible endpoint. Checksums are computed only when an
    operation requires them, which is Cloudflare's documented setting for R2 with boto3 >= 1.36;
    retries use botocore's `standard` mode (exponential backoff with jitter, 5 attempts)."""
    boto3 = importlib.import_module("boto3")
    config_cls = importlib.import_module("botocore.config").Config
    config = config_cls(
        retries={"max_attempts": 5, "mode": "standard"},
        connect_timeout=10,
        read_timeout=60,
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
        s3={"addressing_style": "path"},
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
        config=config,
    )


class S3Backend:
    """An S3-compatible bucket. `client` is injectable (tests pass an in-memory fake); otherwise one
    is built on first use from the constructor's endpoint and credentials."""

    name = "s3"

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        prefix: str = DEFAULT_PREFIX,
        client: Any = None,
    ) -> None:
        if not bucket:
            raise StoreConfigError("S3Backend needs a bucket name")
        self.bucket = bucket
        self.endpoint_url = endpoint_url
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            if not (self.endpoint_url and self._access_key_id and self._secret_access_key):
                raise StoreConfigError("S3Backend needs an endpoint and credentials to build a client")
            self._client = build_s3_client(self.endpoint_url, self._access_key_id, self._secret_access_key)
        return self._client

    def _key(self, key: str) -> str:
        return self.prefix + _check_key(key)

    def put(self, key: str, data: bytes, *, immutable: bool = False) -> None:
        full = self._key(key)
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": full, "Body": data}
        if immutable:
            # An atomic create-if-absent (RFC 7232 If-None-Match on PUT). The HEAD first makes
            # the refusal independent of the endpoint honouring the header: a replay of identical
            # bytes is a no-op, different bytes are refused, either way before any PUT.
            if self.exists(key):
                self._confirm_same(key, data)
                return
            kwargs["IfNoneMatch"] = "*"
        client = self.client
        try:
            client.put_object(**kwargs)
        except Exception as exc:
            code, status = _error_code(exc)
            if immutable and (code in _CONFLICT or status in (409, 412)):
                self._confirm_same(key, data)
                return
            raise StoreWriteError(f"write to {self.uri(key)} failed: {exc}") from exc

    def _confirm_same(self, key: str, data: bytes) -> None:
        if self.get(key) != data:
            raise ImmutableObjectExists(f"{self.uri(key)} already exists with different content")

    def get(self, key: str) -> bytes:
        client = self.client
        try:
            response = client.get_object(Bucket=self.bucket, Key=self._key(key))
            body: bytes = response["Body"].read()
        except Exception as exc:
            code, status = _error_code(exc)
            if code in _MISSING or status == 404:
                raise FileNotFoundError(self.uri(key)) from exc
            raise StoreReadError(f"read of {self.uri(key)} failed: {exc}") from exc
        return body

    def exists(self, key: str) -> bool:
        client = self.client
        try:
            client.head_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:
            code, status = _error_code(exc)
            if code in _MISSING or status == 404:
                return False
            raise StoreReadError(f"head of {self.uri(key)} failed: {exc}") from exc
        return True

    def list(self, prefix: str) -> list[str]:
        full = self.prefix + prefix
        keys: list[str] = []
        client = self.client
        try:
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=full, Delimiter="/"):
                keys.extend(str(obj["Key"])[len(self.prefix) :] for obj in page.get("Contents") or [])
        except Exception as exc:
            raise StoreReadError(f"list of {self.uri(prefix)} failed: {exc}") from exc
        return sorted(keys)

    def delete(self, key: str) -> None:
        client = self.client
        try:
            client.delete_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:
            raise StoreWriteError(f"delete of {self.uri(key)} failed: {exc}") from exc

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{self.prefix}{key}"


# -------------------------------------------------------------------------------- selection
def store_mode(env: Mapping[str, str] | None = None) -> str:
    """`SNAPSHOT_STORE`, lower-cased; unset or empty means `local`."""
    env = os.environ if env is None else env
    return (env.get(ENV_MODE) or "local").strip().lower() or "local"


def backend_from_env(root: pathlib.Path, env: Mapping[str, str] | None = None) -> ObjectBackend:
    """The backend `SNAPSHOT_STORE` selects. `local` roots files at `root`. `s3` needs
    `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` and either `R2_ENDPOINT` or
    `R2_ACCOUNT_ID` (the endpoint is then `https://<account>.r2.cloudflarestorage.com`, as in
    `infra/scripts/backup.sh`); a missing one raises `StoreConfigError` naming all of them."""
    env = os.environ if env is None else env
    mode = store_mode(env)
    if mode == "local":
        return LocalBackend(root)
    if mode != "s3":
        raise StoreConfigError(f"{ENV_MODE}={mode!r}: expected one of {', '.join(MODES)}")
    endpoint = (env.get(ENV_ENDPOINT) or "").strip()
    account = (env.get(ENV_ACCOUNT_ID) or "").strip()
    if not endpoint and account:
        endpoint = f"https://{account}.r2.cloudflarestorage.com"
    values = {
        ENV_BUCKET: (env.get(ENV_BUCKET) or "").strip(),
        ENV_ACCESS_KEY_ID: (env.get(ENV_ACCESS_KEY_ID) or "").strip(),
        ENV_SECRET_ACCESS_KEY: (env.get(ENV_SECRET_ACCESS_KEY) or "").strip(),
        f"{ENV_ENDPOINT} or {ENV_ACCOUNT_ID}": endpoint,
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise StoreConfigError(f"{ENV_MODE}=s3 but unset: {', '.join(missing)}")
    prefix = env.get(ENV_PREFIX)
    return S3Backend(
        values[ENV_BUCKET],
        endpoint_url=endpoint,
        access_key_id=values[ENV_ACCESS_KEY_ID],
        secret_access_key=values[ENV_SECRET_ACCESS_KEY],
        prefix=DEFAULT_PREFIX if prefix is None else prefix,
    )


__all__ = [
    "DEFAULT_PREFIX",
    "ImmutableObjectExists",
    "LocalBackend",
    "ObjectBackend",
    "S3Backend",
    "StoreConfigError",
    "StoreError",
    "StoreReadError",
    "StoreWriteError",
    "backend_from_env",
    "build_s3_client",
    "store_mode",
]
