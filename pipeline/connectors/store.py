"""Snapshot / normalised / event / source_run store (docs/20 §2, §3.2, docs/21 §4).

Layout under a data root (default `data/`), identical as object keys when the backend is a bucket:

    snapshots/{source_id}/{retrieved_at}.{ext}        raw bytes as fetched — evidence, never published
    normalized/{source_id}/{retrieved_at}.parquet     canonical records            (publishable)
    events/{source_id}/{retrieved_at}.parquet         change events vs previous    (publishable)
    held/{source_id}/{retrieved_at}.parquet           normalised output of a held run (not publishable)
    runs/{source_id}/{retrieved_at}.json              source_run record

`QuarantineStore` roots everything under `data/quarantine/` and reports `publishable = False`;
the runner picks it for `reuse: restricted | unknown` sources, so their bytes can never land in
`normalized/` or `events/` (docs/21 §8 "nothing", CLAUDE.md).

Where the bytes go is the backend's business (`pipeline/connectors/objectstore.py`): files under
the root by default, or an S3-compatible bucket (Cloudflare R2) when `SNAPSHOT_STORE=s3` —
`open_store()` reads that choice from the environment; `Store()` is always local. The `*_path`
methods return the object's location *as a path under `root`*; the object key is that path
relative to `base_root`. With the local backend the path is the file; with the S3 backend it
names the key and is not a local file, so read through `exists`/`read_*` and record `locate()`.

Snapshots are immutable: rewriting one with different bytes raises `ImmutableObjectExists`. A run
record is the commit marker — the runner writes it last, and every reader (`runs`,
`previous_normalized`, the loader via the run's `ts`) starts from run records, so an output left
behind by a run that crashed before its record is never read as a result (docs/20 §12).
"""

from __future__ import annotations

import datetime as dt
import io
import json
import pathlib
from typing import Any

import pandas as pd

from pipeline.connectors.base import json_default, to_parquet_safe
from pipeline.connectors.objectstore import LocalBackend, ObjectBackend, backend_from_env
from pipeline.connectors.registry import ROOT

DATA_DIR = ROOT / "data"
PUBLISHABLE_SUBDIRS = ("normalized", "events")
QUARANTINE_DIR = "quarantine"


def ts_token(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


class Store:
    publishable = True
    #: Sub-tree of the data root this store writes under (`QuarantineStore`: `quarantine`).
    subdir: str | None = None

    def __init__(self, root: pathlib.Path = DATA_DIR, backend: ObjectBackend | None = None) -> None:
        self.base_root = pathlib.Path(root)
        self.root = self.base_root / self.subdir if self.subdir else self.base_root
        self.backend: ObjectBackend = backend if backend is not None else LocalBackend(self.base_root)
        self._run_cache: dict[str, dict[str, Any] | None] = {}

    # paths ---------------------------------------------------------------
    def snapshot_path(self, source_id: str, ts: str, ext: str) -> pathlib.Path:
        return self.root / "snapshots" / source_id / f"{ts}.{ext}"

    def normalized_path(self, source_id: str, ts: str) -> pathlib.Path:
        return self.root / "normalized" / source_id / f"{ts}.parquet"

    def events_path(self, source_id: str, ts: str) -> pathlib.Path:
        return self.root / "events" / source_id / f"{ts}.parquet"

    def held_path(self, source_id: str, ts: str) -> pathlib.Path:
        return self.root / "held" / source_id / f"{ts}.parquet"

    def run_path(self, source_id: str, ts: str) -> pathlib.Path:
        return self.root / "runs" / source_id / f"{ts}.json"

    def key(self, path: pathlib.Path) -> str:
        """The object key for a path under this store: the path relative to `base_root`, so the
        quarantine tree keeps its `quarantine/` prefix in a bucket too."""
        try:
            return pathlib.Path(path).relative_to(self.base_root).as_posix()
        except ValueError:
            raise RuntimeError(f"{path} is outside the store root {self.base_root}") from None

    def locate(self, path: pathlib.Path) -> str:
        """Where the object lives, for run records and logs: the file path (local) or `s3://...`."""
        return self.backend.uri(self.key(path))

    # writes --------------------------------------------------------------
    def guard(self, path: pathlib.Path) -> None:
        """Every write stays inside this store's root; a non-publishable store's root is the
        quarantine tree, so a gated source can never reach `data/normalized` or `data/events`."""
        try:
            path.relative_to(self.root)
        except ValueError:
            raise RuntimeError(f"refusing to write {path} outside the store root {self.root}") from None
        if not self.publishable and QUARANTINE_DIR not in path.parts:
            raise RuntimeError(f"refusing to write publishable output {path} from a non-publishable store")

    def write_snapshot(self, source_id: str, ts: str, ext: str, content: bytes) -> pathlib.Path:
        p = self.snapshot_path(source_id, ts, ext)
        self.guard(p)
        self.backend.put(self.key(p), content, immutable=True)
        return p

    def write_parquet(self, path: pathlib.Path, df: pd.DataFrame) -> pathlib.Path:
        self.guard(path)
        buf = io.BytesIO()
        to_parquet_safe(df).to_parquet(buf, index=False)
        self.backend.put(self.key(path), buf.getvalue())
        return path

    def write_run(self, source_id: str, ts: str, record: dict[str, Any]) -> pathlib.Path:
        p = self.run_path(source_id, ts)
        self.guard(p)
        body = json.dumps(record, indent=1, default=json_default, ensure_ascii=False).encode("utf-8")
        key = self.key(p)
        self._run_cache.pop(key, None)
        self.backend.put(key, body)
        return p

    def delete(self, path: pathlib.Path) -> None:
        """Remove one non-snapshot output (the runner's rollback of a run that failed mid-write)."""
        self.guard(path)
        if path.is_relative_to(self.root / "snapshots"):
            raise RuntimeError(f"refusing to delete immutable snapshot {path}")
        self.backend.delete(self.key(path))

    # reads ---------------------------------------------------------------
    def exists(self, path: pathlib.Path) -> bool:
        return self.backend.exists(self.key(path))

    def read_bytes(self, path: pathlib.Path) -> bytes:
        return self.backend.get(self.key(path))

    def read_parquet(self, path: pathlib.Path) -> pd.DataFrame:
        return pd.read_parquet(io.BytesIO(self.read_bytes(path)))

    def read_json(self, path: pathlib.Path) -> Any:
        return json.loads(self.read_bytes(path).decode("utf-8"))

    def _run_entries(self, source_id: str) -> list[tuple[str, dict[str, Any]]]:
        """`(ts, record)` per run record, oldest first. The listing is fresh on every call; bodies
        are cached per key (a record changes only through `write_run`, which evicts it), so the
        three reads a run makes cost one LIST each and each body is fetched once per process."""
        prefix = self.key(self.root / "runs" / source_id) + "/"
        out: list[tuple[str, dict[str, Any]]] = []
        for key in self.backend.list(prefix):
            if not key.endswith(".json"):
                continue
            if key not in self._run_cache:
                try:
                    parsed = json.loads(self.backend.get(key).decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError, FileNotFoundError):
                    parsed = None
                self._run_cache[key] = parsed if isinstance(parsed, dict) else None
            record = self._run_cache[key]
            if record is not None:
                out.append((key.rsplit("/", 1)[-1][: -len(".json")], record))
        return out

    def runs(self, source_id: str) -> list[dict[str, Any]]:
        return [record for _, record in self._run_entries(source_id)]

    def successful_runs(self, source_id: str) -> list[dict[str, Any]]:
        return [r for r in self.runs(source_id) if r.get("status") in ("ok", "unchanged")]

    def last_snapshot_sha(self, source_id: str) -> str | None:
        for r in reversed(self.runs(source_id)):
            sha = (r.get("snapshot") or {}).get("sha256")
            if sha and r.get("status") in ("ok", "unchanged", "partial"):
                return str(sha)
        return None

    def previous_normalized(self, source_id: str) -> tuple[pd.DataFrame | None, str | None]:
        """Latest normalised parquet written by a successful (non-held) run. The object is found
        from the run's own `ts` (the key the runner wrote it under), not from the location the
        record carries, so a record written on another host or backend still resolves."""
        for ts, r in reversed(self._run_entries(source_id)):
            if r.get("status") != "ok" or not (r.get("outputs") or {}).get("normalized"):
                continue
            path = self.normalized_path(source_id, ts)
            if self.exists(path):
                return self.read_parquet(path), str(r.get("id"))
        return None, None

    def dq_history(self, source_id: str) -> list[dict[str, Any]]:
        return [r["stats"] for r in self.successful_runs(source_id) if r.get("stats")]


class QuarantineStore(Store):
    publishable = False
    subdir = QUARANTINE_DIR


def open_store(root: pathlib.Path | None = None) -> Store:
    """The store the environment selects (`SNAPSHOT_STORE`, docs/60 §5): local files under `root`
    (default `DATA_DIR`) or the S3-compatible bucket. Raises `StoreConfigError` rather than falling
    back to local when `s3` is selected without its settings."""
    base = pathlib.Path(root) if root is not None else DATA_DIR
    return Store(base, backend=backend_from_env(base))
