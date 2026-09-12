"""File-backed snapshot / normalised / event / source_run store (docs/20 §3.2, docs/21 §4).

Layout under a data root (default `data/`):

    snapshots/{source_id}/{retrieved_at}.{ext}        raw bytes as fetched — evidence, never published
    normalized/{source_id}/{retrieved_at}.parquet     canonical records            (publishable)
    events/{source_id}/{retrieved_at}.parquet         change events vs previous    (publishable)
    held/{source_id}/{retrieved_at}.parquet           normalised output of a held run (not publishable)
    runs/{source_id}/{retrieved_at}.json              source_run record

`QuarantineStore` roots everything under `data/quarantine/` and reports `publishable = False`;
the runner picks it for `reuse: restricted | unknown` sources, so their bytes can never land in
`normalized/` or `events/` (docs/21 §8 "nothing", CLAUDE.md).
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any

import pandas as pd

from pipeline.connectors.base import json_default, to_parquet_safe
from pipeline.connectors.registry import ROOT

DATA_DIR = ROOT / "data"
PUBLISHABLE_SUBDIRS = ("normalized", "events")


def ts_token(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


class Store:
    publishable = True

    def __init__(self, root: pathlib.Path = DATA_DIR) -> None:
        self.root = root

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

    # writes --------------------------------------------------------------
    def write_snapshot(self, source_id: str, ts: str, ext: str, content: bytes) -> pathlib.Path:
        p = self.snapshot_path(source_id, ts, ext)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def write_parquet(self, path: pathlib.Path, df: pd.DataFrame) -> pathlib.Path:
        if path.parts[len(self.root.parts)] in PUBLISHABLE_SUBDIRS and not self.publishable:
            raise RuntimeError(f"refusing to write publishable output {path} from a non-publishable store")
        path.parent.mkdir(parents=True, exist_ok=True)
        to_parquet_safe(df).to_parquet(path, index=False)
        return path

    def write_run(self, source_id: str, ts: str, record: dict[str, Any]) -> pathlib.Path:
        p = self.run_path(source_id, ts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(record, indent=1, default=json_default, ensure_ascii=False), encoding="utf-8")
        return p

    # reads ---------------------------------------------------------------
    def runs(self, source_id: str) -> list[dict[str, Any]]:
        d = self.root / "runs" / source_id
        if not d.exists():
            return []
        out = []
        for p in sorted(d.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue
        return out

    def successful_runs(self, source_id: str) -> list[dict[str, Any]]:
        return [r for r in self.runs(source_id) if r.get("status") in ("ok", "unchanged")]

    def last_snapshot_sha(self, source_id: str) -> str | None:
        for r in reversed(self.runs(source_id)):
            sha = (r.get("snapshot") or {}).get("sha256")
            if sha and r.get("status") in ("ok", "unchanged", "partial"):
                return str(sha)
        return None

    def previous_normalized(self, source_id: str) -> tuple[pd.DataFrame | None, str | None]:
        """Latest normalised parquet written by a successful (non-held) run."""
        for r in reversed(self.runs(source_id)):
            path = (r.get("outputs") or {}).get("normalized")
            if r.get("status") == "ok" and path and pathlib.Path(path).exists():
                return pd.read_parquet(path), str(r.get("id"))
        return None, None

    def dq_history(self, source_id: str) -> list[dict[str, Any]]:
        return [r["stats"] for r in self.successful_runs(source_id) if r.get("stats")]


class QuarantineStore(Store):
    publishable = False

    def __init__(self, root: pathlib.Path = DATA_DIR) -> None:
        super().__init__(root / "quarantine")
