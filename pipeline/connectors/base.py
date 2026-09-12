"""Connector base contract (docs/20 §3.1, docs/04 E-4/E-17, docs/21 §3 provenance quartet).

A connector is three pure steps:

    fetch()            -> RawSnapshot         network, bytes in, nothing parsed
    parse(RawSnapshot) -> list[dict]          source-shaped rows, never touches the network
    normalize(rows)    -> DataFrame           canonical schema + provenance on every row

`fetch` never runs in CI; `parse` and `normalize` run on recorded fixtures (docs/04 E-6).
Every emitted record carries `source_id, source_url, retrieved_at, licence_id, raw`
(`CLAUDE.md`; docs/04 DA-2) plus a `record_id` = `{source_id}:{source_record_id}` that is stable
across runs (docs/20 §3.1: queue id, notice id, or a content hash of the identifying columns —
the strategy is documented in each connector's docstring).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

import pandas as pd

from pipeline.connectors.http import PoliteSession
from pipeline.connectors.registry import SourceEntry
from pipeline.normalize import CANONICAL_COLUMNS as _PROPOSAL_BASE
from pipeline.normalize import load_status_map

Kind = Literal["proposal", "opportunity"]
Egress = Literal["plain", "browser", "residential", "api_key"]
SnapshotMode = Literal["full", "incremental"]

PROVENANCE = ("source_id", "source_url", "retrieved_at", "licence_id")

# docs/21 §3.1 proposal fields as already produced by pipeline/normalize.py, with the store
# spelling `licence_id` (docs/04 §10) and the jsonb-style `raw` payload appended.
PROPOSAL_COLUMNS: list[str] = [c if c != "licence" else "licence_id" for c in _PROPOSAL_BASE] + ["raw"]

# docs/21 §3.3 opportunity fields (kind, issuer, title, jurisdiction, technologies, open_at,
# due_at, status) plus identity, provenance and the audit columns every record carries.
OPPORTUNITY_COLUMNS: list[str] = [
    "record_id",
    "source_id",
    "source_record_id",
    "source_url",
    "retrieved_at",
    "licence_id",
    "kind",
    "issuer",
    "title",
    "summary",
    "jurisdiction",
    "technologies",
    "capacity_sought_mw",
    "budget_amount",
    "budget_currency",
    "open_at",
    "due_at",
    "status",
    "status_raw",
    "status_rule",
    "identifiers",
    "raw",
]


class ConnectorError(Exception):
    """A fetch failed in a way the run should record as `failed` (docs/04 E-17)."""


class ParseError(ConnectorError):
    """The payload was fetched but is not what the parser expects (e.g. HTML instead of xlsx)."""


class BlockedError(ConnectorError):
    """robots.txt or a challenge page refused the fetch; the run is recorded as `blocked`."""


class GateViolation(Exception):
    """A publication gate would be crossed. Never caught-and-continued (docs/04 E-17)."""


@dataclass
class RawSnapshot:
    """Bytes as fetched plus the HTTP metadata the `snapshot` row needs (docs/21 §4.3)."""

    content: bytes
    content_type: str
    url: str
    retrieved_at: dt.datetime
    http_status: int
    ext: str
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_s: float = 0.0
    requests_made: int = 1
    redacted: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware UTC (docs/04 E-4)")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def retrieved_at_iso(self) -> str:
        return self.retrieved_at.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    @classmethod
    def from_file(
        cls,
        path: pathlib.Path,
        url: str,
        content_type: str = "application/octet-stream",
        retrieved_at: dt.datetime | None = None,
        **meta: Any,
    ) -> RawSnapshot:
        """Build a snapshot from a recorded fixture (tests only; `fetch` never runs in CI)."""
        return cls(
            content=path.read_bytes(),
            content_type=content_type,
            url=url,
            retrieved_at=retrieved_at or dt.datetime(2026, 9, 12, tzinfo=dt.UTC),
            http_status=200,
            ext=path.suffix.lstrip("."),
            meta=meta,
        )


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def json_default(v: Any) -> Any:
    if isinstance(v, (dt.datetime, dt.date, pd.Timestamp)):
        return v.isoformat()
    if isinstance(v, float) and pd.isna(v):
        return None
    if v is pd.NA or v is pd.NaT:
        return None
    if hasattr(v, "item"):  # numpy scalars
        return v.item()
    return str(v)


def raw_json(row: dict[str, Any]) -> str:
    """Serialise a source-shaped row as the jsonb-style `raw` payload (NaN -> null)."""
    clean = {str(k): (None if _isna(v) else v) for k, v in row.items()}
    return json.dumps(clean, default=json_default, ensure_ascii=False, sort_keys=True)


def _isna(v: Any) -> bool:
    if v is None:
        return True
    try:
        return bool(pd.isna(v)) if not isinstance(v, (list, dict, tuple, set)) else False
    except (TypeError, ValueError):
        return False


def content_hash(*parts: Any) -> str:
    """Deterministic `source_record_id` for sources without a stable id (docs/20 §3.1)."""
    key = "|".join("" if _isna(p) else str(p).strip() for p in parts)
    return "h" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]  # noqa: S324 - identity key, not security


class Connector:
    """Base class. Subclasses set the class attributes and implement fetch/parse/normalize."""

    source_id: ClassVar[str]
    kind: ClassVar[Kind]
    egress: ClassVar[Egress] = "plain"
    ext: ClassVar[str] = "bin"
    parser_version: ClassVar[str] = "1.0.0"
    honour_robots: ClassVar[bool] = True
    #: "full": the payload is the whole register, so a row that disappears is a `removed` event.
    #: "incremental": the payload is a window (last N days); rows are upserted onto the previous
    #: normalised snapshot and disappearance means nothing.
    snapshot_mode: ClassVar[SnapshotMode] = "full"
    #: key of this source in its status_map.yaml `sources:` block
    status_key: ClassVar[str] = ""
    #: per-connector status map (docs/04 DA-5); None = pipeline/status_map.yaml
    status_map_path: ClassVar[pathlib.Path | None] = None
    #: "hold": duplicate source_record_id holds the run (docs/04 DA-6);
    #: "suffix": the parser has no stable composite key, duplicates are suffixed #2, #3 in file
    #: order (deterministic) and reported as a DQ warning — must be justified in the docstring.
    dedupe_strategy: ClassVar[Literal["hold", "suffix"]] = "hold"
    #: canonical fields whose null rate is watched (docs/04 DA-6 "null spike")
    dq_required_fields: ClassVar[tuple[str, ...]] = ()
    #: source columns that feed canonical fields; their removal holds the run (schema drift)
    key_source_columns: ClassVar[tuple[str, ...]] = ()
    #: raw-column names to strip before the snapshot is stored (docs/13 §5.4 rule 1)
    personal_data_columns: ClassVar[tuple[str, ...]] = ()

    def __init__(self, source: SourceEntry, http: PoliteSession | None = None) -> None:
        if source.id != self.source_id:
            raise ValueError(f"{type(self).__name__} is bound to {self.source_id}, got {source.id}")
        self.source = source
        self.http = http or PoliteSession(rate_limits={source.host: source.max_rps})
        self._status_map: dict[str, Any] | None = None

    # ------------------------------------------------------------------ contract
    def fetch(self) -> RawSnapshot:
        raise NotImplementedError

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        raise NotImplementedError

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        raise NotImplementedError

    def redact(self, content: bytes) -> bytes:
        """Strip contact identifiers before the snapshot is stored; default: nothing to strip."""
        return content

    # ------------------------------------------------------------------ helpers
    @property
    def status_map(self) -> dict[str, Any]:
        if self._status_map is None:
            path = self.status_map_path
            self._status_map = load_status_map(path) if path else load_status_map()
        return self._status_map

    @property
    def columns(self) -> list[str]:
        return PROPOSAL_COLUMNS if self.kind == "proposal" else OPPORTUNITY_COLUMNS

    def finalize(self, df: pd.DataFrame, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        """Stamp provenance, build record_id, attach `raw`, order columns, resolve duplicates.

        `rows` and `df` must be aligned row-for-row (the parser's rows are the `raw` payload).
        """
        if len(rows) != len(df):
            raise ParseError(f"normalize produced {len(df)} rows for {len(rows)} parsed rows")
        out = df.copy().reset_index(drop=True)
        out["source_id"] = self.source_id
        out["retrieved_at"] = raw.retrieved_at_iso
        out["licence_id"] = self.source.licence_id
        if "source_url" not in out.columns:
            out["source_url"] = raw.url
        out["source_url"] = out["source_url"].fillna(raw.url)
        if "licence" in out.columns:
            out = out.drop(columns=["licence"])
        out["source_record_id"] = out["source_record_id"].astype("string").str.strip()
        missing_id = out["source_record_id"].isna() | (out["source_record_id"] == "")
        if bool(missing_id.any()):
            raise ParseError(f"{int(missing_id.sum())} rows without source_record_id")
        out["record_id"] = self.source_id + ":" + out["source_record_id"]
        dup_n = out.groupby("record_id").cumcount()
        if self.dedupe_strategy == "suffix":
            out.loc[dup_n > 0, "record_id"] = out["record_id"] + "#" + (dup_n + 1).astype(str)
        out.attrs["duplicates_resolved"] = int((dup_n > 0).sum()) if self.dedupe_strategy == "suffix" else 0
        out["raw"] = [raw_json(r) for r in rows]
        for c in self.columns:
            if c not in out.columns:
                out[c] = None
        out = out[self.columns]
        for c in ("source_record_id", "record_id", "source_url", "licence_id", "source_id", "raw"):
            out[c] = out[c].astype("string")
        return out


def to_parquet_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Parquet needs homogeneous columns: object columns become strings, lists become JSON."""
    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object:
            out[c] = (
                out[c]
                .map(
                    lambda v: (
                        json.dumps(v, default=json_default)
                        if isinstance(v, (list, dict))
                        else (None if _isna(v) else str(v))
                    )
                )
                .astype("string")
            )
    return out
